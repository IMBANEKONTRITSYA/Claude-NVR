from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..db import get_db
from ..models import FaceEvent, Person
from ..auth import get_current_user
from ..params import days_param, limit_param

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("/kpi")
async def kpi(_=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_count = (await db.execute(select(func.count(FaceEvent.id)).where(FaceEvent.ts >= today))).scalar() or 0
    unique_today = (await db.execute(
        select(func.count(func.distinct(FaceEvent.person_id))).where(FaceEvent.ts >= today)
    )).scalar() or 0
    total_persons = (await db.execute(select(func.count(Person.id)))).scalar() or 0
    known = (await db.execute(select(func.count(Person.id)).where(Person.status == "known"))).scalar() or 0
    return {
        "detections_today": today_count,
        "unique_persons_today": unique_today,
        "total_persons": total_persons,
        "known_persons": known,
    }


@router.get("/by-day")
async def by_day(days: int = days_param(14), _=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(func.date_trunc("day", FaceEvent.ts).label("d"), func.count(FaceEvent.id))
        .where(FaceEvent.ts >= since)
        .group_by("d").order_by("d")
    )
    return [{"day": str(row[0].date()), "count": row[1]} for row in r.all()]


@router.get("/by-hour")
async def by_hour(_=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=7)
    r = await db.execute(
        select(func.extract("hour", FaceEvent.ts).label("h"), func.count(FaceEvent.id))
        .where(FaceEvent.ts >= since)
        .group_by("h").order_by("h")
    )
    data = {int(h): c for h, c in r.all()}
    return [{"hour": h, "count": data.get(h, 0)} for h in range(24)]


@router.get("/heatmap")
async def heatmap(days: int = days_param(30), _=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """Тепловая карта: день недели (0=Пн..6=Вс) × час (0..23)."""
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(
            func.extract("isodow", FaceEvent.ts).label("dow"),
            func.extract("hour", FaceEvent.ts).label("h"),
            func.count(FaceEvent.id),
        )
        .where(FaceEvent.ts >= since)
        .group_by("dow", "h")
    )
    grid = [[0] * 24 for _ in range(7)]
    for dow, h, c in r.all():
        grid[int(dow) - 1][int(h)] = c
    return {"grid": grid}


@router.get("/top-persons")
async def top_persons(days: int = days_param(30), limit: int = limit_param(10), _=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(Person.id, Person.name, Person.status, func.count(FaceEvent.id).label("c"))
        .join(FaceEvent, FaceEvent.person_id == Person.id)
        .where(FaceEvent.ts >= since)
        .group_by(Person.id).order_by(func.count(FaceEvent.id).desc()).limit(limit)
    )
    return [{"id": x[0], "name": x[1] or f"Неизвестный #{x[0]}", "status": x[2], "count": x[3]} for x in r.all()]
