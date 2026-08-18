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
from ..schemas import SegmentOut, TimelineOut
from ..services import archive_timeline as timeline_svc
from ..services import export as export_svc
from ..services import thumbs as thumbs_svc
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


@router.get("/timeline", response_model=TimelineOut)
async def timeline(
    camera_id: int = Query(..., ge=1),
    date_from: datetime = Query(...),
    date_to: datetime = Query(...),
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """Шкала архива одной камеры за окно времени (ТЗ §5).

    Отвечает на вопрос, которого до сих пор нельзя было задать системе:
    **в какие минуты у камеры есть запись**. Таблица сегментов на него не
    отвечает — она показывает то, что есть, а дыра это то, чего нет, и в
    списке строк она не видна.

    Отдаёт три вещи разом, одним запросом: склеенные диапазоны покрытия
    (для отрисовки шкалы), упорядоченную цепочку сегментов (по ней плеер
    идёт через границы файлов без остановки) и «сколько записано за окно».

    Границы окна наивные и трактуются как UTC — так же, как их хранит
    `video_segments` и как их принимает экспорт фрагмента: приведение к
    поясу браузера где-нибудь по дороге сдвинуло бы шкалу относительно
    самих записей.
    """
    date_from = export_svc.as_naive_utc(date_from)
    date_to = export_svc.as_naive_utc(date_to)
    window_sec = (date_to - date_from).total_seconds()
    if window_sec <= 0:
        raise HTTPException(422, "Конец периода должен быть позже начала")
    if window_sec > timeline_svc.MAX_TIMELINE_HOURS * 3600:
        raise HTTPException(
            422,
            f"Окно шкалы не может быть длиннее {timeline_svc.MAX_TIMELINE_HOURS} часов",
        )

    # +1 к потолку — чтобы отличить «ровно потолок» от «упёрлись»: без
    # этого выдача из ровно MAX строк неотличима от обрезанной.
    q = timeline_svc.timeline_query(
        camera_id, date_from, date_to,
        limit=timeline_svc.MAX_TIMELINE_SEGMENTS + 1,
    )
    segments = (await db.execute(q)).scalars().all()
    truncated = len(segments) > timeline_svc.MAX_TIMELINE_SEGMENTS
    if truncated:
        segments = segments[: timeline_svc.MAX_TIMELINE_SEGMENTS]

    ranges = timeline_svc.clamp_ranges(
        timeline_svc.merge_coverage(segments), date_from, date_to,
    )
    return TimelineOut(
        camera_id=camera_id,
        date_from=date_from,
        date_to=date_to,
        ranges=[{"start": r.start, "end": r.end} for r in ranges],
        segments=segments,
        recorded_sec=timeline_svc.recorded_seconds(ranges),
        truncated=truncated,
    )


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


@router.get("/thumb/{seg_id}")
async def segment_thumb(
    seg_id: int,
    db: AsyncSession = Depends(get_db),
    _=Depends(require_role_query("admin", "operator")),
):
    """Миниатюра кадра сегмента архива (ТЗ §7).

    Токен в query string и роль из БД — по той же причине, что и у
    скачивания сегмента: адрес подставляется в `<img src>`, заголовок к
    нему не прикрепить, а кадр записи по матрице прав §18 наблюдателю
    закрыт так же, как сама запись.

    Ответ кэшируется браузером на сутки: закрытый сегмент иммутабелен, а
    выдача архива — это до 200 миниатюр, которые иначе перезапрашивались
    бы при каждом уточнении фильтра.
    """
    seg = await db.get(VideoSegment, seg_id)
    if not seg:
        raise HTTPException(404, "Сегмент не найден")
    # within_media_root — та же проверка, что и у экспорта: путь берётся из
    # БД, но именно он уходит аргументом в ffmpeg, и строка, указывающая за
    # пределы медиа-каталога, означает повреждение данных, а не запись.
    if not export_svc.within_media_root(seg.file_path, settings.MEDIA_PATH) \
            or not os.path.exists(seg.file_path):
        raise HTTPException(404, "Файл не найден")

    dst = thumbs_svc.thumb_path(settings.MEDIA_PATH, seg_id)
    try:
        await thumbs_svc.ensure(seg.file_path, dst, seg.duration_sec)
    except thumbs_svc.ThumbError as exc:
        # 404, а не 500: битый или пустой сегмент — штатное состояние архива
        # (обрыв RTSP на первой секунде файла), и выдача должна показать
        # строку без картинки, а не ошибку на всю страницу.
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(dst, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=86400"})


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
