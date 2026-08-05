import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import VideoSegment, FaceEvent
from ..auth import require_role, require_role_query
from ..schemas import SegmentOut

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("/segments", response_model=list[SegmentOut])
async def list_segments(
    camera_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    event_type: str | None = None,
    person_id: int | None = None,
    limit: int = 200,
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    q = select(VideoSegment).order_by(VideoSegment.started_at.desc()).limit(limit)
    if camera_id:
        q = q.where(VideoSegment.camera_id == camera_id)
    if event_type:
        q = q.where(VideoSegment.event_type == event_type)
    if date_from:
        q = q.where(VideoSegment.started_at >= date_from)
    if date_to:
        q = q.where(VideoSegment.started_at <= date_to)
    if person_id:
        # Сегмент относится к персоне, если на той же камере есть событие лица
        # этой персоны во временном диапазоне сегмента.
        exists_q = (
            select(FaceEvent.id)
            .where(
                FaceEvent.person_id == person_id,
                FaceEvent.camera_id == VideoSegment.camera_id,
                FaceEvent.ts >= VideoSegment.started_at,
                FaceEvent.ts <= VideoSegment.ended_at,
            )
            .exists()
        )
        q = q.where(exists_q)
    r = await db.execute(q)
    return r.scalars().all()


@router.get("/file/{seg_id}")
async def download_segment(
    seg_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role_query("admin", "operator")),
):
    """Скачивание файла сегмента архива.

    Токен в query string (ссылка открывается браузером напрямую), но роль
    сверяется с БД: разжалованный в viewer не должен скачивать записи ещё
    30 минут до истечения access-токена — матрица прав ТЗ отдаёт архив
    только admin/operator.
    """
    seg = await db.get(VideoSegment, seg_id)
    if not seg or not os.path.exists(seg.file_path):
        raise HTTPException(404, "Файл не найден")
    return FileResponse(seg.file_path, media_type="video/mp4", filename=os.path.basename(seg.file_path))
