"""Занесение дописанных сегментов слоя записи в `video_segments`.

Отделено от `record_layer.py` (только stdlib) и от `worker.py` (тянет
`cv2`/`insightface`): здесь нужен один SQLAlchemy, поэтому путь «файл на
диске → строка архива» проверяется тестом на настоящей БД, а не на моке.

Модель `VideoSegment` передаётся параметром, а не импортируется: в
`worker.py` она объявлена рядом с моделями, которым нужен `pgvector`, и
импорт отсюда потащил бы за собой всю тяжёлую цепочку.
"""
import logging

from sqlalchemy import func, select

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
                       now: float, camera_model,
                       from_timestamp, settle_sec: float = SEGMENT_SETTLE_SEC,
                       min_bytes: int = MIN_SEGMENT_BYTES) -> int:
    """Заносит в архив все дописанные сегменты, которых там ещё нет.

    `from_timestamp` — функция unix-время → `datetime`, ожидаемый БД
    (в воркере это naive-UTC, см. соглашение в `fileage.py`).

    Идемпотентна: повторный вызов на тех же файлах ничего не добавляет —
    сверка идёт по `file_path`, а он уникален (в имени unix-время начала
    сегмента). Именно поэтому индексация может спокойно стоять в цикле
    менеджера, который выполняется каждые ~10 с.

    `camera_model` обязателен, и вот почему. Идентификатор камеры берётся
    **из имени файла**, а `video_segments.camera_id` — внешний ключ на
    `cameras.id`. Файлы удалённой камеры (§3) остаются на диске, её строки
    архива сносит `ON DELETE CASCADE`, и следующий же проход пытается
    завести их заново — с идентификатором, которого в `cameras` больше
    нет. Postgres отвечает отказом внешнего ключа, а вставка идёт **одной
    транзакцией на весь проход**, поэтому вместе с осиротевшими не
    заносятся и сегменты всех остальных камер: одно удаление камеры
    останавливало индексацию архива целиком и навсегда (файлы-сироты со
    временем не исчезают). Сегменты камер, которых нет, здесь пропускаются;
    их файлы убирает `worker.prune_orphan_media()`.

    Гонка «камеру удалили между этим запросом и коммитом» возможна и
    оставлена намеренно: она даёт один неудачный проход, следующий уже
    видит камеру удалённой и пропускает её сегменты сам.
    """
    # Граница «докуда архив уже заполнен», по камерам. Без неё каждый
    # проход снимал `stat` со всех файлов каталога и спрашивал у БД все их
    # пути разом — на объекте это миллион файлов и миллион параметров в
    # `IN`, каждые ~10 с, ради нуля новых строк (замеры — в
    # `perf/bench_segment_scan.py`).
    #
    # Обратное преобразование времени берётся у самой `from_timestamp`
    # (`from_timestamp(0.0)` — начало эпохи в том виде времени, в котором
    # вызывающий кладёт метки в БД), а не через `utcfromtimestamp`:
    # предположение о UTC здесь было бы лишним, а `started_at` в базе
    # получен ровно этой функцией из времени в имени файла, поэтому
    # обратный переход точен.
    with session_factory() as s:
        epoch = from_timestamp(0.0)
        after = {
            cam_id: (last - epoch).total_seconds()
            for cam_id, last in s.execute(
                select(segment_model.camera_id, func.max(segment_model.started_at))
                .group_by(segment_model.camera_id)
            ).all()
            if last is not None
        }

    found = collect_complete_segments(segments_dir, now, settle_sec, after=after)
    if not found:
        return 0

    fresh = [s for s in found if s["size_bytes"] >= min_bytes]
    if not fresh:
        return 0

    added = 0
    with session_factory() as s:
        # Спрашиваются только камеры, встреченные на диске: список конечный
        # (не больше числа камер объекта), а ответ «этих нет» не зависит от
        # того, сколько камер заведено всего.
        seen_cams = {x["camera_id"] for x in fresh}
        live_cams = set(
            s.execute(
                select(camera_model.id).where(camera_model.id.in_(seen_cams))
            ).scalars().all()
        )
        gone = seen_cams - live_cams
        if gone:
            fresh = [x for x in fresh if x["camera_id"] in live_cams]
            logger.info(
                "сегменты удалённых камер пропущены при индексации",
                extra={"camera_ids": sorted(gone)},
            )
            if not fresh:
                return 0

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
