"""Выборка сегментов архива — единственное место, где она собирается.

Вынесено из `routers/archive.py` не ради красоты, а чтобы бенчмарк
норматива §26 («поиск по архиву ≤ 5 с») мерил **тот самый** запрос, который
выполняет приложение, а не его копию. Урок цикла 26: копия production-SQL
в бенчмарке молча расходится с оригиналом, и замер начинает подтверждать
несуществующее поведение — ровно так поиск по фото 25 циклов «укладывался
в норматив», ни разу не задев HNSW-индекс.

`perf/bench.py --only archive` импортирует `segments_query` отсюда и
компилирует её в SQL. Если фильтр здесь изменится, бенчмарк измерит
изменённый запрос без единой правки в самом бенчмарке.
"""
from datetime import datetime

from sqlalchemy import select

from ..models import FaceEvent, VideoSegment


def segments_query(
    camera_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    event_type: str | None = None,
    person_id: int | None = None,
    limit: int = 200,
):
    """Запрос списка сегментов архива с фильтрами страницы «Видеоархив».

    Порядок — от свежих к старым: оператор ищет вчерашнее событие, а не
    первое за две недели retention.
    """
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
    return q
