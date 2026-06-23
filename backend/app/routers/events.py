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
    _=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = (
        select(FaceEvent, Person.name, Person.status)
        .outerjoin(Person, Person.id == FaceEvent.person_id)
        .order_by(FaceEvent.ts.desc())
        .limit(limit)
    )
    if camera_id:
        q = q.where(FaceEvent.camera_id == camera_id)
    if is_known is not None:
        q = q.where(FaceEvent.is_known == is_known)
    r = await db.execute(q)
    out = []
    for ev, pname, pstatus in r.all():
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
        ))
    return out
