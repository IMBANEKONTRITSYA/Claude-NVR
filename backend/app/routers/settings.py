"""Системные настройки (хранятся в БД, читаются воркером на лету)."""
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import Setting
from ..auth import require_role
from ..profiles import PROFILES, profile_settings
from ..services.encryption import (
    SECRET_SETTING_KEYS,
    decrypt_setting,
    encrypt_setting,
)

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
}

ENUMS = {
    "performance_profile": set(PROFILES),
    "face_model": {"buffalo_s", "buffalo_l"},
    "upscale_mode": {"manual", "avatar", "all"},
}


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
    frame_skip: int | None = None
    motion_prefilter: int | None = None
    idle_fps: int | None = None
    face_model: str | None = None
    upscale_mode: str | None = None
    cluster_interval_min: int | None = None
    detect_width: int | None = None
    record_segment_min: int | None = None


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
    """Доступные профили производительности и их параметры (ТЗ 18.9)."""
    return {
        "profiles": PROFILES,
        "titles": {
            "economy": "Экономный — слабое железо (Intel N100), 4–8 камер",
            "standard": "Стандартный — Core i3 / Ryzen 3, 8–12 камер",
            "maximum": "Максимальный — Core i5+ / GPU, 12–16 камер",
        },
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
