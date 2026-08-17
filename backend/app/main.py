import asyncio
import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi import HTTPException
from sqlalchemy import func, select, text
from .db import engine, Base, SessionLocal
from .models import User
from .auth import hash_password, get_user_from_query_token
from .config import settings, insecure_secret_problems
from .profiles import DEFAULT_PROFILE, profile_settings
from .routers import auth as r_auth, users as r_users, cameras as r_cameras
from .routers import persons as r_persons, events as r_events, archive as r_archive
from .routers import stats as r_stats, reports as r_reports, ws as r_ws
from .routers import search as r_search
from .routers import settings as r_settings
from .routers import audit as r_audit
from .routers import system as r_system
from .services import thumbs as thumbs_svc
from .audit import AuditMiddleware
from .logging_utils import configure_logging

logger = configure_logging("facewatch.backend")


async def apply_schema_migrations(conn):
    """Создание таблиц и лёгкие миграции существующих БД.

    Вынесено из `lifespan` отдельной функцией, чтобы блок можно было
    прогнать в тесте на настоящем Postgres, а не только через полный старт
    приложения (см. `tests/test_startup_migrations.py`).

    Вызывается внутри `engine.begin()`, то есть **одной транзакцией**: либо
    применяется всё, либо ничего. Из этого следует правило для всего, что
    добавляется сюда с `try/except`: в Postgres любой упавший оператор
    переводит транзакцию в состояние aborted, и перехват исключения его не
    отменяет — до конца блока не выполнится ни один следующий оператор, а
    на выходе откатится всё, включая `create_all` и миграции выше. Поэтому
    операторы, отказ которых допустим, обязаны выполняться через
    `conn.begin_nested()` (SAVEPOINT): откат идёт до точки сохранения, и
    транзакция остаётся рабочей (см. docs/reviews/REVIEW_LOG.md, цикл 23).
    """
    await conn.run_sync(Base.metadata.create_all)
    # Лёгкие миграции для существующих БД (create_all не добавляет колонки)
    for stmt in (
        "ALTER TABLE face_events ADD COLUMN IF NOT EXISTS orig_snapshot_path varchar(500)",
        "ALTER TABLE face_events ADD COLUMN IF NOT EXISTS enhanced boolean DEFAULT false",
        "ALTER TABLE face_events ALTER COLUMN enhanced SET DEFAULT false",
        "ALTER TABLE persons ADD COLUMN IF NOT EXISTS notes text",
        "ALTER TABLE persons ADD COLUMN IF NOT EXISTS alert_on_detection boolean DEFAULT false",
        # ADD COLUMN IF NOT EXISTS выше — no-op на БД, где колонка уже была добавлена
        # раньше без DEFAULT (до этого фикса): raw SQL INSERT в routers/persons.py,
        # который не указывает эту колонку явно, падал NotNullViolationError на
        # каждый вызов. SET DEFAULT применяется безусловно, чтобы починить и такие
        # уже развёрнутые БД, не только свежие (см. docs/reviews/REVIEW_LOG.md, цикл 5).
        "ALTER TABLE persons ALTER COLUMN alert_on_detection SET DEFAULT false",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS sub_rtsp_url_enc text",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS motion_sensitivity integer",
        # ТЗ 18.7: ONVIF-события движения/присутствия людей вместо MOG2-префильтра.
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_enabled boolean DEFAULT false",
        "ALTER TABLE cameras ALTER COLUMN onvif_enabled SET DEFAULT false",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_host varchar(255)",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_port integer",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_username varchar(120)",
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_password_enc text",
        "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at timestamp DEFAULT now()",
        # SPEC §2/§3: режим камеры. Для уже развёрнутых БД колонка добавляется
        # со значением 'analytics' — до этой миграции аналитика шла по всем
        # включённым камерам, и молча выключить её на обновлении значило бы
        # потерять распознавание без единого сообщения. Дефолт для НОВЫХ строк
        # переключается следующим оператором на 'record_only', как требует ТЗ.
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS mode varchar(20) DEFAULT 'analytics'",
        "UPDATE cameras SET mode = 'analytics' WHERE mode IS NULL",
        "ALTER TABLE cameras ALTER COLUMN mode SET DEFAULT 'record_only'",
        "ALTER TABLE cameras ALTER COLUMN mode SET NOT NULL",
        # SPEC §5, §21: глубина хранения по камерам. NULL намеренно — «следовать
        # за глобальным retention_days», см. комментарий в models.py. Поэтому
        # здесь нет ни DEFAULT, ни UPDATE: обновление не должно ничего менять
        # в поведении существующих камер.
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS retention_days integer",
        # SPEC §6: расписание детекции. Ни DEFAULT, ни UPDATE — NULL здесь
        # означает «круглосуточно», то есть поведение существующих камер
        # после обновления не меняется (см. models.py).
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS detection_schedule jsonb",
        # SPEC §6: «запись только при движении (опционально)». Опция —
        # значит выключена у всех существующих камер: включение задним
        # числом означало бы удаление уже записанного архива на объекте,
        # который об этой функции ещё не знает.
        "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS record_on_motion boolean DEFAULT false",
        "UPDATE cameras SET record_on_motion = false WHERE record_on_motion IS NULL",
        "ALTER TABLE cameras ALTER COLUMN record_on_motion SET DEFAULT false",
        "ALTER TABLE cameras ALTER COLUMN record_on_motion SET NOT NULL",
        # SPEC §21: фактический расход и выбор старейших сегментов под
        # циклическую перезапись. Существующим строкам ставится 0 («размер
        # неизвестен»), а не фактический размер файла: обход архива на 120
        # камерах за 14 дней — сотни тысяч stat() на старте приложения.
        # Индексация дописывает размер только новым сегментам, и расчёт
        # расхода это переживает — он идёт по последним суткам.
        "ALTER TABLE video_segments ADD COLUMN IF NOT EXISTS size_bytes bigint DEFAULT 0",
        "UPDATE video_segments SET size_bytes = 0 WHERE size_bytes IS NULL",
        "ALTER TABLE video_segments ALTER COLUMN size_bytes SET DEFAULT 0",
        "ALTER TABLE video_segments ALTER COLUMN size_bytes SET NOT NULL",
    ):
        await conn.execute(text(stmt))
    # HNSW-индексы pgvector для быстрого поиска по эмбеддингам (≤5с на 100k лиц).
    # Единственные операторы блока, отказ которых не должен ронять старт:
    # индекс — ускорение, без него приложение работает. Каждый идёт в своём
    # SAVEPOINT — см. docstring выше: без него отказ (например,
    # `access method "hnsw" does not exist` на pgvector < 0.5.0 — ровно тот
    # случай, ради которого здесь стоит `except`) откатывал весь блок, а в
    # логе оставалось одно предупреждение про индекс.
    for stmt in (
        "CREATE INDEX IF NOT EXISTS idx_face_events_embedding ON face_events "
        "USING hnsw (embedding vector_cosine_ops)",
        "CREATE INDEX IF NOT EXISTS idx_persons_centroid ON persons "
        "USING hnsw (centroid vector_cosine_ops)",
    ):
        try:
            async with conn.begin_nested():
                await conn.execute(text(stmt))
        except Exception:
            logger.warning("не удалось создать HNSW-индекс", exc_info=True, extra={"statement": stmt})


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not settings.ALLOW_INSECURE_DEFAULT_SECRETS:
        problems = insecure_secret_problems()
        if problems:
            for p in problems:
                logger.critical(p)
            raise RuntimeError(
                "Запуск остановлен: обнаружены секреты по умолчанию из публичного репозитория "
                "(см. сообщения выше). Заполните .env реальными значениями "
                "(start.bat делает это автоматически при первом запуске) или, только для "
                "локальной отладки, установите ALLOW_INSECURE_DEFAULT_SECRETS=true."
            )
    os.makedirs(settings.MEDIA_PATH, exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "snapshots"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "segments"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "avatars"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "uploads"), exist_ok=True)
    # Миниатюры архива (§7). Каталог создаётся и при генерации, но заведён
    # здесь наравне с остальными: администратор, размечающий диски под §26,
    # должен видеть полный состав медиа-каталога на пустой системе.
    os.makedirs(os.path.join(settings.MEDIA_PATH, thumbs_svc.THUMB_DIR), exist_ok=True)
    async with engine.begin() as conn:
        await apply_schema_migrations(conn)
    async with SessionLocal() as db:
        r = await db.execute(select(User).where(User.username == "admin"))
        if not r.scalar_one_or_none():
            db.add(User(username="admin", password_hash=hash_password(settings.ADMIN_PASSWORD), role="admin"))
            await db.commit()
        # Сид системных настроек по умолчанию
        from .models import Setting
        defaults = {
            "retention_days": str(settings.RETENTION_DAYS_DEFAULT),
            "motion_threshold": "1500",
            "similarity_threshold": "0.45",
            "event_cooldown_sec": "10",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            # SPEC §11: почта выключена, пока администратор не заполнит хост.
            # Порт 587 и starttls — рабочее сочетание для подавляющего
            # большинства релеев, чтобы из формы осталось заполнить только
            # хост, логин и пароль.
            "smtp_host": "",
            "smtp_port": "587",
            "smtp_user": "",
            "smtp_password": "",
            "smtp_tls": "starttls",
            "smtp_from": "",
            "alert_email_to": "",
            "alert_sound_enabled": "0",
            "alert_cooldown_sec": "300",
            "record_segment_min": "5",
            # SPEC §16, §19: потолок CPU слоя аналитики. Ноль — «подобрать
            # автоматически по числу ядер сервера»: значение в БД не может
            # быть верным для всех объектов, а сервер воркер видит сам
            # (worker/ort_threads.py).
            "analytics_threads": "0",
            "performance_profile": DEFAULT_PROFILE,
            # detection_fps, frame_skip, face_model, upscale_mode и т.д.
            **profile_settings(DEFAULT_PROFILE),
        }
        rows = (await db.execute(select(Setting))).scalars().all()
        existing = {s.key for s in rows}
        # SPEC §1: аналитика — «только на N выбранных камерах (по умолчанию 2)».
        # На свежей БД камер нет и предел равен двум. На БД, обновлённой с
        # предыдущей редакции ТЗ, аналитика шла по всем камерам, и миграция
        # выше сохранила им режим analytics — выставить предел в 2 значило бы
        # оставить систему заведомо «сверх предела»: работать она продолжит,
        # но любая правка камеры упиралась бы в отказ, которого администратор
        # ничем не вызывал. Поэтому начальное значение — фактическое число
        # таких камер, не меньше двух; уменьшить его можно в настройках.
        if "analytics_cameras_max" not in existing:
            from .models import Camera
            analytics_now = (await db.execute(
                select(func.count()).select_from(Camera).where(Camera.mode == "analytics")
            )).scalar() or 0
            defaults["analytics_cameras_max"] = str(max(2, analytics_now))
        for k, v in defaults.items():
            if k not in existing:
                db.add(Setting(key=k, value=v))
        # Миграция секретов на шифрование (ТЗ 13). В БД, развёрнутых до этого
        # фикса, telegram_bot_token лежит открытым текстом — и попадает таким
        # в дампы pg_dump, которые backup/run.sh хранит 14 дней. Значение без
        # префикса SECRET_SETTING_PREFIX — как раз такой legacy-plaintext:
        # дошифровываем его на старте, чтобы фикс подействовал сам, а не
        # только после того, как администратор вручную пересохранит форму
        # настроек. Уже зашифрованные значения префикс отсеивает, так что
        # повторный запуск ничего не портит.
        from .services.encryption import encrypt_setting, needs_secret_migration
        for s in rows:
            if needs_secret_migration(s.key, s.value):
                s.value = encrypt_setting(s.value)
                logger.info("секрет настроек зашифрован при миграции", extra={"key": s.key})
        await db.commit()

    # Планировщик отчётов (SPEC §8). Отдельная задача в том же процессе, а
    # не сервис: три расписания на объекте не стоят пятого systemd-юнита
    # (§26). Остановка через Event и ожидание задачи — иначе §13
    # «graceful shutdown» нарушался бы отменой посреди отправки письма.
    #
    # Фоновый цикл гасится флагом окружения только в тестах (его ставит
    # backend/tests/conftest.py). Причина — не «мешает», а
    # недетерминированность: цикл тикает по стенным часам каждые TICK_SEC и
    # закрывает любое расписание с наступившим слотом (по умолчанию 8:00).
    # Тест, заводящий включённое расписание при прогоне после 8 утра,
    # получал бы гонку с этим тиком — слот закрывался бы у него под руками.
    # Саму логику прохода тесты проверяют явными вызовами `run_due_now()`
    # (test_integration_report_schedules.py), поэтому покрытие от снятия
    # фонового цикла не страдает, а прогон становится детерминированным.
    from .services.report_scheduler import scheduler_loop
    reports_stop = asyncio.Event()
    scheduler_disabled = os.environ.get(
        "FACEWATCH_DISABLE_REPORT_SCHEDULER", ""
    ).strip().lower() in ("1", "true", "yes")
    reports_task = (
        None if scheduler_disabled
        else asyncio.create_task(scheduler_loop(reports_stop))
    )
    try:
        yield
    finally:
        reports_stop.set()
        if reports_task is not None:
            try:
                await asyncio.wait_for(reports_task, timeout=20)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                reports_task.cancel()


app = FastAPI(title="FaceWatch API", lifespan=lifespan)

_cors_origins = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    # Список источников из настроек, а не "*": в production фронтенд и API
    # ходят через один nginx (same-origin), CORS нужен только для dev-сервера.
    # Auth — Bearer-токен в заголовке (не cookie), поэтому credentials не нужны.
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditMiddleware)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc: RequestValidationError):
    """Ответ 422 без эхо присланного значения.

    Обработчик FastAPI по умолчанию вкладывает в тело ответа поля `input`
    (то, что прислал клиент) и `ctx`. Отсюда две неприятности, вторая
    обнаружилась вместе с первыми float-полями в API (PTZ, SPEC §4):

    1. **500 вместо 422.** `Infinity`/`NaN` в теле запроса `json.loads`
       принимает, поле их отвергает (`allow_inf_nan=False`) — а вот
       сериализовать их обратно в JSON-ответ уже нельзя: `json.dumps`
       по стандарту JSON отказывается, и обработка запроса падает
       внутренней ошибкой. То есть валидация срабатывала, но клиент
       получал 500.
    2. **Эхо присланного.** Значение, не прошедшее валидацию, возвращалось
       клиенту обратно — для полей, куда попадают учётные данные камеры,
       это лишнее содержимое в ответе и в логах.

    Остаются `loc`, `msg` и `type`: их достаточно, чтобы форма показала, что
    именно не так с каким полем, и все они по определению JSON-безопасны.
    """
    return JSONResponse(
        status_code=422,
        content={"detail": [
            {"loc": list(e.get("loc", ())), "msg": e.get("msg", ""), "type": e.get("type", "")}
            for e in exc.errors()
        ]},
    )

app.include_router(r_auth.router)
app.include_router(r_users.router)
app.include_router(r_cameras.router)
app.include_router(r_persons.router)
app.include_router(r_events.router)
app.include_router(r_archive.router)
app.include_router(r_stats.router)
app.include_router(r_reports.router)
app.include_router(r_search.router)
app.include_router(r_settings.router)
app.include_router(r_audit.router)
app.include_router(r_system.router)
app.include_router(r_ws.router)


@app.get("/api/health")
async def health():
    """Глубокая проверка: БД и Redis должны отвечать."""
    from fastapi.responses import JSONResponse
    from .services.pubsub import get_redis
    status = {"ok": True, "db": "ok", "redis": "ok"}
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:
        status["ok"] = False
        status["db"] = f"error: {str(e)[:80]}"
    try:
        await get_redis().ping()
    except Exception as e:
        status["ok"] = False
        status["redis"] = f"error: {str(e)[:80]}"
    return JSONResponse(status, status_code=200 if status["ok"] else 503)


# Матрица прав (SPEC.md) на уровне каталогов медиа. Раньше эндпоинт ниже
# проверял только валидность токена и раздавал любой из трёх каталогов
# любой аутентифицированной роли — включая `segments`, то есть файлы
# видеоархива, которые по матрице прав (строка «Архив») наблюдателю
# запрещены. Роутер архива это ограничение соблюдает
# (routers/archive.py: и список сегментов, и /api/archive/file/{id}
# требуют admin/operator), но /api/media/segments/<файл> давал обходной
# путь мимо него: имена сегментов детерминированы и легко перебираются
# (worker.py пишет их как `cam{camera_id}_{unix_ts}.mp4`, а id камер
# наблюдателю известны — живой просмотр ему разрешён), так что перебор
# секунд за интересующий период выдаёт наблюдателю записи архива целиком.
# snapshots/avatars остаются доступны всем ролям осознанно: на них
# построены Стена и дашборд, разрешённые наблюдателю той же матрицей.
MEDIA_KIND_ROLES: dict[str, tuple[str, ...]] = {
    "snapshots": ("admin", "operator", "viewer"),
    "avatars": ("admin", "operator", "viewer"),
    "segments": ("admin", "operator"),
}


@app.get("/api/media/{kind}/{name}")
async def media_file(kind: str, name: str, user=Depends(get_user_from_query_token)):
    # Роль берётся из БД (get_user_from_query_token), а не из claim'а токена:
    # набор разрешённых ролей здесь зависит от вида медиа, и разжалованный из
    # operator в viewer не должен скачивать сегменты архива ещё 30 минут до
    # истечения access-токена — ровно то, что MEDIA_KIND_ROLES и запрещает.
    allowed_roles = MEDIA_KIND_ROLES.get(kind)
    if allowed_roles is None:
        raise HTTPException(404)
    if user.role not in allowed_roles:
        raise HTTPException(403, "Недостаточно прав")
    name = os.path.basename(name)  # защита от ../ в имени
    path = os.path.join(settings.MEDIA_PATH, kind, name)
    if not os.path.exists(path):
        raise HTTPException(404)
    # Снимки иммутабельны (улучшенная версия получает новое имя enh_*),
    # так что браузер может кэшировать — Стена и галереи не перекачивают JPEG.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
