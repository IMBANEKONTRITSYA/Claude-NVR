import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import HTTPException
from jose import jwt, JWTError
from sqlalchemy import select, text
from .db import engine, Base, SessionLocal
from .models import User
from .auth import hash_password
from .config import settings, insecure_secret_problems
from .profiles import DEFAULT_PROFILE, profile_settings
from .routers import auth as r_auth, users as r_users, cameras as r_cameras
from .routers import persons as r_persons, events as r_events, archive as r_archive
from .routers import stats as r_stats, reports as r_reports, ws as r_ws
from .routers import search as r_search
from .routers import settings as r_settings
from .routers import audit as r_audit
from .routers import system as r_system
from .audit import AuditMiddleware
from .logging_utils import configure_logging

logger = configure_logging("facewatch.backend")


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
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкие миграции для существующих БД (create_all не добавляет колонки)
        await conn.execute(text(
            "ALTER TABLE face_events ADD COLUMN IF NOT EXISTS orig_snapshot_path varchar(500)"
        ))
        await conn.execute(text(
            "ALTER TABLE face_events ADD COLUMN IF NOT EXISTS enhanced boolean DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE face_events ALTER COLUMN enhanced SET DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE persons ADD COLUMN IF NOT EXISTS notes text"
        ))
        await conn.execute(text(
            "ALTER TABLE persons ADD COLUMN IF NOT EXISTS alert_on_detection boolean DEFAULT false"
        ))
        # ADD COLUMN IF NOT EXISTS выше — no-op на БД, где колонка уже была добавлена
        # раньше без DEFAULT (до этого фикса): raw SQL INSERT в routers/persons.py,
        # который не указывает эту колонку явно, падал NotNullViolationError на
        # каждый вызов. SET DEFAULT применяется безусловно, чтобы починить и такие
        # уже развёрнутые БД, не только свежие (см. docs/reviews/REVIEW_LOG.md, цикл 5).
        await conn.execute(text(
            "ALTER TABLE persons ALTER COLUMN alert_on_detection SET DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS sub_rtsp_url_enc text"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS motion_sensitivity integer"
        ))
        # ТЗ 18.7: ONVIF-события движения/присутствия людей вместо MOG2-префильтра.
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_enabled boolean DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ALTER COLUMN onvif_enabled SET DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_host varchar(255)"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_port integer"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_username varchar(120)"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS onvif_password_enc text"
        ))
        await conn.execute(text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS password_changed_at timestamp DEFAULT now()"
        ))
        # HNSW-индексы pgvector для быстрого поиска по эмбеддингам (≤5с на 100k лиц)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_face_events_embedding ON face_events "
            "USING hnsw (embedding vector_cosine_ops)",
            "CREATE INDEX IF NOT EXISTS idx_persons_centroid ON persons "
            "USING hnsw (centroid vector_cosine_ops)",
        ):
            try:
                await conn.execute(text(stmt))
            except Exception:
                logger.warning("не удалось создать HNSW-индекс", exc_info=True, extra={"statement": stmt})
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
            "alert_cooldown_sec": "300",
            "record_codec": "h264",
            "record_bitrate": "0",
            "record_iframe_only": "0",
            "performance_profile": DEFAULT_PROFILE,
            # detection_fps, frame_skip, face_model, upscale_mode и т.д.
            **profile_settings(DEFAULT_PROFILE),
        }
        rows = (await db.execute(select(Setting))).scalars().all()
        existing = {s.key for s in rows}
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
    yield


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
async def media_file(kind: str, name: str, token: str = Query(...)):
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Не авторизован")
    allowed_roles = MEDIA_KIND_ROLES.get(kind)
    if allowed_roles is None:
        raise HTTPException(404)
    if payload.get("role") not in allowed_roles:
        raise HTTPException(403, "Недостаточно прав")
    name = os.path.basename(name)  # защита от ../ в имени
    path = os.path.join(settings.MEDIA_PATH, kind, name)
    if not os.path.exists(path):
        raise HTTPException(404)
    # Снимки иммутабельны (улучшенная версия получает новое имя enh_*),
    # так что браузер может кэшировать — Стена и галереи не перекачивают JPEG.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
