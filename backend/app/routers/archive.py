import os
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from starlette.background import BackgroundTask
from ..config import settings
from ..db import get_db
from ..models import VideoSegment
from ..auth import require_role, require_role_query
from ..params import limit_param
from ..schemas import SegmentOut
from ..services import export as export_svc
from ..services.archive_query import segments_query

router = APIRouter(prefix="/api/archive", tags=["archive"])


@router.get("/segments", response_model=list[SegmentOut])
async def list_segments(
    camera_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    event_type: str | None = None,
    person_id: int | None = None,
    limit: int = limit_param(200),
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    # Сам запрос живёт в services/archive_query.py: его же импортирует
    # бенчмарк норматива §26, чтобы мерить production-запрос, а не копию.
    q = segments_query(
        camera_id=camera_id, date_from=date_from, date_to=date_to,
        event_type=event_type, person_id=person_id, limit=limit,
    )
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


@router.get("/export")
async def export_fragment(
    camera_id: int = Query(..., ge=1),
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role_query("admin", "operator")),
):
    """Экспорт фрагмента архива по границам времени (ТЗ §5).

    Отдаёт один MP4 на запрошенное окно, даже если оно лежит поверх
    нескольких сегментов. Сборка — только remux (§24 запрещает
    перекодирование архива), детали и ограничение точности реза ключевыми
    кадрами — в `services/export.py`.

    Токен в query string, как и у скачивания сегмента: ссылку открывает сам
    браузер, заголовок к ней не прикрепить. Роль сверяется с БД
    (`require_role_query`), архив по матрице прав §25 — admin/operator.
    """
    date_from = export_svc.as_naive_utc(date_from)
    date_to = export_svc.as_naive_utc(date_to)
    window_sec = (date_to - date_from).total_seconds()
    if window_sec <= 0:
        raise HTTPException(422, "Конец периода должен быть позже начала")
    if window_sec > export_svc.MAX_EXPORT_SECONDS:
        raise HTTPException(
            422,
            f"Фрагмент не может быть длиннее {export_svc.MAX_EXPORT_SECONDS // 60} минут",
        )

    # Отбор по пересечению с окном, а не по `started_at` в окне: сегмент,
    # начавшийся до date_from и закончившийся внутри окна, содержит нужное
    # начало фрагмента — по фильтру «started_at >= date_from» он бы
    # потерялся, и экспорт молча начинался бы с середины.
    q = (
        select(VideoSegment)
        .where(
            VideoSegment.camera_id == camera_id,
            VideoSegment.started_at < date_to,
            VideoSegment.ended_at > date_from,
        )
        .order_by(VideoSegment.started_at)
        .limit(export_svc.MAX_EXPORT_SEGMENTS + 1)
    )
    segments = (await db.execute(q)).scalars().all()
    if len(segments) > export_svc.MAX_EXPORT_SEGMENTS:
        raise HTTPException(
            422,
            "Слишком много сегментов в периоде — выберите более короткий фрагмент",
        )

    # Файл может отсутствовать на диске (ротация по retention успела его
    # удалить между выборкой и сборкой) или лежать вне медиа-каталога —
    # см. within_media_root(). Пропускаем такие, а не падаем: фрагмент из
    # оставшихся сегментов полезнее отказа целиком.
    usable = [
        s for s in segments
        if export_svc.within_media_root(s.file_path, settings.MEDIA_PATH)
        and os.path.exists(s.file_path)
    ]
    pieces = export_svc.plan_pieces(usable, date_from, date_to)
    if not pieces:
        raise HTTPException(404, "За выбранный период записей не найдено")

    stamp = date_from.strftime("%Y%m%d-%H%M%S")
    out_name = f"cam{camera_id}_{stamp}_{int(window_sec)}s.mp4"
    workdir = export_svc.make_workdir()
    try:
        out_path = await export_svc.build_fragment(pieces, workdir, out_name)
    except export_svc.ExportError as exc:
        export_svc.cleanup_workdir(workdir)
        raise HTTPException(500, str(exc)) from exc
    except Exception:
        export_svc.cleanup_workdir(workdir)
        raise

    # Каталог удаляется задачей ответа — то есть после того, как файл
    # полностью ушёл клиенту. Удалить его здесь означало бы отдавать
    # FileResponse на уже удалённый файл.
    return FileResponse(
        out_path,
        media_type="video/mp4",
        filename=out_name,
        background=BackgroundTask(export_svc.cleanup_workdir, workdir),
    )
