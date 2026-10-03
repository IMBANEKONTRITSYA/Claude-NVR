"""Системные настройки (хранятся в БД, читаются воркером на лету)."""
import anyio
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import Setting
from ..auth import get_current_user, require_role
from ..profiles import PROFILES, profile_settings
from .cameras import DEFAULT_ANALYTICS_MAX
from ..services.autoconfig import MAX_ANALYTICS_CAMERAS
from ..services.encryption import (
    SECRET_SETTING_KEYS,
    decrypt_setting,
    encrypt_setting,
)
from ..services.mailer import TLS_MODES, MailerError, send_email, split_recipients

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Допустимые ключи и валидаторы (значение хранится строкой)
SCHEMA: dict[str, tuple] = {
    "retention_days": (int, 1, 3650),
    "motion_threshold": (int, 100, 1_000_000),
    "similarity_threshold": (float, 0.1, 0.9),
    "detection_fps": (int, 1, 30),
    "event_cooldown_sec": (int, 1, 300),
    "alert_cooldown_sec": (int, 10, 86400),
    "telegram_bot_token": (str,),     # просто строка, может быть пустой
    "telegram_chat_id": (str,),
    # SPEC §11 «Настройки уведомлений: Telegram, email, звук». Почта нужна
    # трём разделам сразу: §6 (алерты при детекции), §8 (авто-отправка
    # отчётов по расписанию) и §11. Пустой smtp_host отключает почту целиком.
    "smtp_host": (str,),
    "smtp_port": (int, 1, 65535),
    "smtp_user": (str,),
    "smtp_password": (str,),          # секрет, шифруется (см. encryption.py)
    "smtp_tls": (str,),               # none | starttls | ssl
    "smtp_from": (str,),              # пусто → берётся smtp_user
    "alert_email_to": (str,),         # получатели алертов §6, через запятую
    # §6 «Алерты при детекции (Telegram, email, звук)»: звук — это клиентская
    # часть (Стена распознавания), но включается он тем же экраном настроек,
    # что и остальные два канала, поэтому флаг лежит здесь.
    "alert_sound_enabled": (int, 0, 1),
    # Профиль производительности и его параметры (ТЗ 18)
    "performance_profile": (str,),
    "frame_skip": (int, 0, 20),
    "motion_prefilter": (int, 0, 1),
    "idle_fps": (int, 1, 30),
    "face_model": (str,),
    "upscale_mode": (str,),           # manual | avatar | all
    "cluster_interval_min": (int, 1, 1440),
    "detect_width": (int, 320, 1920),
    # Слой записи (SPEC §20: «Сегменты 5–10 минут»). Кодек, битрейт и GOP
    # больше не настраиваются: запись идёт remux'ом основного потока как
    # есть, SPEC §24 явно запрещает перекодирование архива.
    "record_segment_min": (int, 5, 10),
    # SPEC §1: «Камеры аналитики: от 0 до N (конфигурируется, зависит от
    # возможностей сервера)».
    #
    # **Здесь стояло 16, и это был хардкод, прямо запрещённый §22**
    # («Запрещено: хардкодить количество камер или аналитики»).
    # Обоснованием служил «§23 (2-3 ядра на аналитику)» — раздела с таким
    # содержанием в действующем ТЗ нет вовсе: §23 это «Документация», а
    # удельная стоимость аналитики задана §16 вилкой «0.5-1.5 ядра на
    # камеру». То есть предел был унаследован от удалённой редакции ТЗ
    # вместе со ссылкой на неё.
    #
    # Практический вред был не в самой цифре, а в том, что она связывала:
    # калькулятор §16 на сервере из §20 («Большой объект», 2× CPU)
    # насчитывает по ресурсам около 29 каналов, но предложить и применить
    # мог только 16, потому что выше не пускала валидация.
    #
    # Верхняя граница теперь та же, что у числа камер вообще (§1: «от 12
    # до 250+»): предел аналитики — вопрос ресурсов сервера, а не
    # константы в схеме. Тем, что администратор не заведёт больше, чем
    # машина тянет, ведает не валидация, а три вещи сразу:
    # предложение автоконфигурации (`services/autoconfig.py`),
    # предупреждение рядом с ним и бюджет потоков ORT
    # (`worker/ort_threads.py`), который делит отведённую слою половину
    # машины на фактическое число камер.
    "analytics_cameras_max": (int, 1, MAX_ANALYTICS_CAMERAS),
    # SPEC §16, §19: потолок CPU слоя аналитики — сколько потоков отдать
    # пулу ONNX Runtime. 0 — считать автоматически из числа ядер сервера и
    # `analytics_cameras_max` (worker/ort_threads.py). Верхняя граница 128 —
    # с запасом над §20 (2× CPU, 64 потока); пул больше числа ядер не
    # ускоряет ничего, поэтому воркер дополнительно режет значение по
    # фактическому числу ядер.
    "analytics_threads": (int, 0, 128),
    # SPEC §5, §21: циклическая перезапись и алерты по заполнению диска.
    # Верхняя граница 50% у порога перезаписи — выше него «свободное место»
    # перестаёт быть аварийным запасом и превращается в способ выбросить
    # половину архива настройкой в одно поле.
    "disk_min_free_pct": (int, 1, 50),
    "disk_warn_pct": (int, 50, 99),
    "disk_crit_pct": (int, 50, 99),
}

ENUMS = {
    "performance_profile": set(PROFILES),
    "face_model": {"buffalo_s", "buffalo_l"},
    "upscale_mode": {"manual", "avatar", "all"},
    "smtp_tls": set(TLS_MODES),
}


# Настройки, видимые интерфейсу под любой ролью (см. GET /api/settings/client).
# Белый список: добавление ключа сюда — осознанное решение «это не секрет».
CLIENT_SETTING_KEYS = frozenset({"alert_sound_enabled"})

assert not (CLIENT_SETTING_KEYS & SECRET_SETTING_KEYS), (
    "секретная настройка не может отдаваться через /api/settings/client"
)


class SettingsUpdate(BaseModel):
    # extra="forbid": по умолчанию pydantic молча выбрасывает неизвестные
    # поля, и PUT с опечаткой в ключе (или с настройкой, убранной из ТЗ, —
    # record_codec/record_bitrate/record_iframe_only после цикла 24)
    # отвечал 200 «сохранено», не сохранив ничего. Явный отказ 422 не даёт
    # администратору решить, что настройка применилась.
    model_config = {"extra": "forbid"}

    retention_days: int | None = None
    motion_threshold: int | None = None
    similarity_threshold: float | None = None
    detection_fps: int | None = None
    event_cooldown_sec: int | None = None
    alert_cooldown_sec: int | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_password: str | None = None
    smtp_tls: str | None = None
    smtp_from: str | None = None
    alert_email_to: str | None = None
    alert_sound_enabled: int | None = None
    frame_skip: int | None = None
    motion_prefilter: int | None = None
    idle_fps: int | None = None
    face_model: str | None = None
    upscale_mode: str | None = None
    cluster_interval_min: int | None = None
    detect_width: int | None = None
    record_segment_min: int | None = None
    analytics_cameras_max: int | None = None
    analytics_threads: int | None = None
    disk_min_free_pct: int | None = None
    disk_warn_pct: int | None = None
    disk_crit_pct: int | None = None


def _visible(rows) -> dict[str, str]:
    """Настройки в виде, пригодном для админки: секреты расшифровываются.

    Эндпоинт и так admin-only и и до этого фикса отдавал токен бота открытым
    (форма настроек сохраняется целиком, поэтому маска вместо значения
    затёрла бы токен при первом же сохранении) — шифрование здесь про
    хранение, не про передачу: цель в том, чтобы токена не было открытым
    текстом в БД и в дампах pg_dump, которые backup/run.sh держит 14 дней.
    """
    out = {}
    for s in rows:
        out[s.key] = decrypt_setting(s.value) if s.key in SECRET_SETTING_KEYS else s.value
    return out


@router.get("")
async def get_settings(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Setting))).scalars().all()
    return _visible(rows)


@router.get("/client")
async def client_settings(_=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Настройки, которые нужны интерфейсу любой роли (SPEC §18).

    Отдельный эндпоинт, а не `GET /api/settings`: тот admin-only и отдаёт в
    том числе расшифрованные секреты (токен бота, пароль SMTP). Звуковой
    алерт §6 должен работать у оператора и наблюдателя на Стене, поэтому
    сюда попадает только явно перечисленный набор несекретных флагов —
    список белый, чтобы новая секретная настройка не утекла сюда сама.
    """
    rows = (await db.execute(select(Setting).where(Setting.key.in_(CLIENT_SETTING_KEYS)))).scalars().all()
    out = {s.key: s.value for s in rows}
    return {k: out.get(k, "") for k in sorted(CLIENT_SETTING_KEYS)}


@router.put("")
async def update_settings(payload: SettingsUpdate, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    data = payload.model_dump(exclude_none=True)
    for key, val in data.items():
        spec = SCHEMA[key]
        caster = spec[0]
        try:
            casted = caster(val)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Некорректное значение для {key}")
        if len(spec) == 3:
            lo, hi = spec[1], spec[2]
            if not (lo <= casted <= hi):
                raise HTTPException(400, f"{key} должно быть в диапазоне [{lo}, {hi}]")
        allowed = ENUMS.get(key)
        if allowed and casted not in allowed:
            raise HTTPException(400, f"{key}: допустимые значения — {', '.join(sorted(allowed))}")
        stored = str(casted)
        if key in SECRET_SETTING_KEYS:
            stored = encrypt_setting(stored)
        existing = await db.get(Setting, key)
        if existing:
            existing.value = stored
        else:
            db.add(Setting(key=key, value=stored))
    # Ручная правка параметра профиля переводит его в режим «своя настройка»
    if any(k in PROFILE_TUNABLES for k in data):
        prof = await db.get(Setting, "performance_profile")
        if prof and prof.value in PROFILES:
            prof.value = "custom"
    await db.commit()
    rows = (await db.execute(select(Setting))).scalars().all()
    return _visible(rows)


PROFILE_TUNABLES = {
    "detection_fps", "frame_skip", "motion_prefilter", "idle_fps",
    "face_model", "upscale_mode", "cluster_interval_min", "detect_width",
}


@router.get("/profiles")
async def list_profiles(_=Depends(require_role("admin"))):
    """Доступные профили производительности и их параметры (SPEC §19).

    Подписи описывают, что профиль делает, а не марку процессора. Прежние
    («Intel N100, 4–8 камер», «Core i5+ / GPU, 12–16 камер») достались от
    удалённой редакции ТЗ, где вся система была на 16 камер и все они шли
    в аналитику. В Scale Edition это вводит администратора в заблуждение
    дважды: числа читаются как относящиеся к 120 камерам записи, а
    «Core i5+ / GPU» — как несовместимость с целевым сервером (2×
    E5-2670, §23), у которого GPU нет вовсе.
    """
    return {
        "profiles": PROFILES,
        "titles": {
            "economy": "Экономный — детектор только по движению, каждый 4-й "
                       "кадр, лёгкая модель. Наименьшая нагрузка на ядро",
            "standard": "Стандартный (по умолчанию) — детектор по движению, "
                        "лёгкая модель, в фоне улучшается только аватар",
            "maximum": "Максимальный — детектор по КАЖДОМУ кадру без "
                       "префильтра, тяжёлая модель, кадр 960 px. Самый "
                       "дорогой по CPU из трёх",
        },
        # SPEC §19: «применяются только к слою аналитики; слой записи всегда
        # в режиме remux». Без этой строки администратор целевого объекта
        # разумно предполагает, что профиль относится ко всем 120 камерам.
        "note": (
            "Профиль применяется только к слою аналитики и затрагивает "
            f"камеры в режиме «аналитика» (по умолчанию {DEFAULT_ANALYTICS_MAX}). "
            "Запись всех камер идёт через MediaMTX без декодирования и от "
            "профиля не зависит. По §23 рост числа камер аналитики сверх "
            "8–10 требует GPU, которого на целевом сервере нет."
        ),
    }


@router.post("/profile/{name}")
async def apply_profile(name: str, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Применяет профиль: перезаписывает управляемые им параметры."""
    if name not in PROFILES:
        raise HTTPException(400, f"Неизвестный профиль: {name}")
    values = profile_settings(name)
    values["performance_profile"] = name
    for key, val in values.items():
        existing = await db.get(Setting, key)
        if existing:
            existing.value = val
        else:
            db.add(Setting(key=key, value=val))
    await db.commit()
    rows = (await db.execute(select(Setting))).scalars().all()
    return _visible(rows)


@router.post("/test-email")
async def test_email(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Пробное письмо на адреса из alert_email_to (SPEC §11).

    Без такой кнопки единственный способ узнать, что почта настроена
    неправильно, — дождаться реального алерта и не получить его. Ошибки
    отдаются администратору дословно (класс исключения smtplib), чтобы
    отличить «не тот пароль» от «порт закрыт».
    """
    rows = _visible((await db.execute(select(Setting))).scalars().all())
    host = rows.get("smtp_host", "")
    to = rows.get("alert_email_to", "")
    if not host:
        raise HTTPException(400, "Не задан smtp_host")
    if not split_recipients(to):
        raise HTTPException(400, "Не задан ни один получатель (alert_email_to)")
    try:
        port = int(rows.get("smtp_port") or 587)
    except ValueError:
        raise HTTPException(400, "Некорректный smtp_port")
    # SMTP-сессия синхронная и может занять до SMTP_TIMEOUT_SEC: в event loop
    # это заблокировало бы все остальные запросы бэкенда на 15 секунд.
    try:
        sent = await anyio.to_thread.run_sync(
            lambda: send_email(
                host, port, rows.get("smtp_user", ""), rows.get("smtp_password", ""),
                rows.get("smtp_tls", "starttls"), rows.get("smtp_from", ""), to,
                "FaceWatch: тестовое сообщение",
                "Это проверка настроек почты FaceWatch. "
                "Если письмо дошло — алерты и отчёты будут приходить сюда же.",
            )
        )
    except MailerError as e:
        raise HTTPException(502, str(e))
    if not sent:
        raise HTTPException(400, "Почта не настроена")
    return {"ok": True, "recipients": split_recipients(to)}


@router.post("/test-telegram")
async def test_telegram(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = _visible((await db.execute(select(Setting))).scalars().all())
    token = rows.get("telegram_bot_token", "")
    chat = rows.get("telegram_chat_id", "")
    if not token or not chat:
        raise HTTPException(400, "Не заданы telegram_bot_token и telegram_chat_id")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(url, json={"chat_id": chat, "text": "FaceWatch: тестовое сообщение"})
        except httpx.HTTPError as e:
            raise HTTPException(502, f"Не удалось отправить: {e}")
    if resp.status_code != 200:
        raise HTTPException(502, f"Telegram API: {resp.status_code} {resp.text[:200]}")
    return {"ok": True}
