from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import FaceEvent
from ..auth import get_current_user
from ..schemas import FaceEventOut

router = APIRouter(prefix="/api/events", tags=["events"])


@router.get("", response_model=list[FaceEventOut])
async def recent_events(
    limit: int = 100,
    camera_id: int | None = None,
    is_known: bool | None = None,
    _=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    q = select(FaceEvent).order_by(FaceEvent.ts.desc()).limit(limit)
    if camera_id:
        q = q.where(FaceEvent.camera_id == camera_id)
    if is_known is not None:
        q = q.where(FaceEvent.is_known == is_known)
    r = await db.execute(q)
    return r.scalars().all()
