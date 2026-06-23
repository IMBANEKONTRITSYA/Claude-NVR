"""Системные настройки (хранятся в БД, читаются воркером на лету)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import Setting
from ..auth import require_role

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Допустимые ключи и валидаторы (значение хранится строкой)
SCHEMA = {
    "retention_days": (int, 1, 3650),
    "motion_threshold": (int, 100, 1_000_000),
    "similarity_threshold": (float, 0.1, 0.9),
    "detection_fps": (int, 1, 30),
}


class SettingsUpdate(BaseModel):
    retention_days: int | None = None
    motion_threshold: int | None = None
    similarity_threshold: float | None = None
    detection_fps: int | None = None


@router.get("")
async def get_settings(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Setting))).scalars().all()
    return {s.key: s.value for s in rows}


@router.put("")
async def update_settings(payload: SettingsUpdate, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    data = payload.model_dump(exclude_none=True)
    for key, val in data.items():
        caster, lo, hi = SCHEMA[key]
        try:
            casted = caster(val)
        except (TypeError, ValueError):
            raise HTTPException(400, f"Некорректное значение для {key}")
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
