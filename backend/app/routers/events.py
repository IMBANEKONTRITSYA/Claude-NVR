from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import FaceEvent, Person
from ..auth import get_current_user
from ..schemas import FaceEventRich

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=list[FaceEventRich])
async def recent_events(
    limit: int = 100,
    camera_id: int | None = None,
    is_known: bool | None = None,
    before_id: int | None = None,   # курсор для бесконечного скролла Стены
    _=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    limit = min(max(1, limit), 200)
    q = (
        select(FaceEvent, Person.name, Person.status, Person.tags)
        .outerjoin(Person, Person.id == FaceEvent.person_id)
        .order_by(FaceEvent.id.desc())
        .limit(limit)
    )
    if camera_id:
        q = q.where(FaceEvent.camera_id == camera_id)
    if is_known is not None:
        q = q.where(FaceEvent.is_known == is_known)
    if before_id:
        q = q.where(FaceEvent.id < before_id)
    r = await db.execute(q)
    out = []
    for ev, pname, pstatus, ptags in r.all():
        name = pname if (pname and pname.strip()) else f"Неизвестный #{ev.person_id}"
        out.append(FaceEventRich(
            id=ev.id,
            camera_id=ev.camera_id,
            person_id=ev.person_id,
            name=name,
            ts=ev.ts,
            snapshot_path=ev.snapshot_path,
            is_known=ev.is_known,
            bbox=ev.bbox,
            # SPEC §15: «Фильтры и поиск по ленте» — Стена фильтрует
            # ленту по тегам персоны, поэтому они едут вместе с событием,
            # а не подтягиваются карточкой на каждый элемент ленты.
            tags=list(ptags or []),
        ))
    return out
