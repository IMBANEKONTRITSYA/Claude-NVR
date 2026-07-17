"""Системные настройки (хранятся в БД, читаются воркером на лету)."""
import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import Setting
from ..auth import require_role

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
}


class SettingsUpdate(BaseModel):
    retention_days: int | None = None
    motion_threshold: int | None = None
    similarity_threshold: float | None = None
    detection_fps: int | None = None
    event_cooldown_sec: int | None = None
    alert_cooldown_sec: int | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None


@router.get("")
async def get_settings(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Setting))).scalars().all()
    return {s.key: s.value for s in rows}


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
        existing = await db.get(Setting, key)
        if existing:
            existing.value = str(casted)
        else:
            db.add(Setting(key=key, value=str(casted)))
    await db.commit()
    rows = (await db.execute(select(Setting))).scalars().all()
    return {s.key: s.value for s in rows}


@router.post("/test-telegram")
async def test_telegram(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = {s.key: s.value for s in (await db.execute(select(Setting))).scalars().all()}
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
