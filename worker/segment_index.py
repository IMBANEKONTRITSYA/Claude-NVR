"""Занесение дописанных сегментов слоя записи в `video_segments`.

Отделено от `record_layer.py` (только stdlib) и от `worker.py` (тянет
`cv2`/`insightface`): здесь нужен один SQLAlchemy, поэтому путь «файл на
диске → строка архива» проверяется тестом на настоящей БД, а не на моке.

Модель `VideoSegment` передаётся параметром, а не импортируется: в
`worker.py` она объявлена рядом с моделями, которым нужен `pgvector`, и
импорт отсюда потащил бы за собой всю тяжёлую цепочку.
"""
import logging

from sqlalchemy import select

from record_layer import SEGMENT_SETTLE_SEC, collect_complete_segments

logger = logging.getLogger("facewatch.worker")

# Тип события для непрерывной записи слоя записи (SPEC §5: пишутся все
# основные потоки постоянно, а не по движению). Прежние значения
# `motion`/`face` остаются валидными для сегментов, записанных до цикла 24, —
# фильтр архива по типу продолжает работать на смешанной истории.
CONTINUOUS = "continuous"

# Сегменты меньше этого размера — почти наверняка обрывки: MediaMTX создаёт
# файл сразу при появлении потока и может закрыть его пустым, если камера
# отвалилась в ту же секунду. Строка архива на нулевой файл хуже, чем её
# отсутствие: она выглядит как доступная запись и отдаёт 404 при скачивании.
MIN_SEGMENT_BYTES = 1024


def index_new_segments(session_factory, segment_model, segments_dir: str, *,
                       now: float, from_timestamp, settle_sec: float = SEGMENT_SETTLE_SEC,
                       min_bytes: int = MIN_SEGMENT_BYTES) -> int:
    """Заносит в архив все дописанные сегменты, которых там ещё нет.

    `from_timestamp` — функция unix-время → `datetime`, ожидаемый БД
    (в воркере это naive-UTC, см. соглашение в `fileage.py`).

    Идемпотентна: повторный вызов на тех же файлах ничего не добавляет —
    сверка идёт по `file_path`, а он уникален (в имени unix-время начала
    сегмента). Именно поэтому индексация может спокойно стоять в цикле
    менеджера, который выполняется каждые ~10 с.
    """
    found = collect_complete_segments(segments_dir, now, settle_sec)
    if not found:
        return 0

    fresh = [s for s in found if s["size_bytes"] >= min_bytes]
    if not fresh:
        return 0

    added = 0
    with session_factory() as s:
        known = set(
            s.execute(
                select(segment_model.file_path).where(
                    segment_model.file_path.in_([x["file_path"] for x in fresh])
                )
            ).scalars().all()
        )
        for seg in fresh:
            if seg["file_path"] in known:
                continue
            started = from_timestamp(seg["started_ts"])
            ended = from_timestamp(seg["ended_ts"])
            s.add(segment_model(
                camera_id=seg["camera_id"],
                started_at=started,
                ended_at=ended,
                file_path=seg["file_path"],
                event_type=CONTINUOUS,
                duration_sec=int(max(0.0, seg["ended_ts"] - seg["started_ts"])),
                # SPEC §21: размер берётся здесь, в момент, когда файл уже
                # дописан (`collect_complete_segments` отдаёт только такие),
                # и больше не меняется. Считать его позже пришлось бы
                # обходом архива на диске.
                size_bytes=int(seg["size_bytes"]),
            ))
            added += 1
        if added:
            s.commit()
    if added:
        logger.info("сегменты записи занесены в архив", extra={"segments": added})
    return added
