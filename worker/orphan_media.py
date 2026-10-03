"""Медиа-файлы, потерявшие свою строку в БД (SPEC §3 «удаление камер», §5).

Вся остальная уборка архива идёт **от строки к файлу**: `cleanup_old`,
циклическая перезапись и режим «запись только при движении» выбирают строки
`video_segments` и удаляют файл вместе со строкой (`worker._drop_segments`).
Механизм полный ровно до одного случая — когда строку удаляет не воркер.

`video_segments.camera_id` объявлен `ForeignKey(..., ondelete="CASCADE")`:
удаление камеры через веб-интерфейс (§3) сносит **все** её строки архива
одним запросом Postgres, мимо воркера и мимо `_drop_segments`. Файлы при
этом остаются, и удалить их больше некому: ни retention, ни перезаписи
нечего выбирать — строк нет. `fileage.prune_media` каталог `segments/`
чистит только по маске `*_tmp.mp4`, дописанных сегментов не касается.
На камере с суточным расходом ~21.6 ГБ (§16, `Mbps × 10.8`) и глубиной
хранения 14 суток это ~300 ГБ, которые остаются на диске навсегда и при
этом невидимы: подсчёт архива идёт по строкам, а место они занимают
настоящее — циклическая перезапись начнёт резать записи **живых** камер,
освобождая место под архив камеры, которой нет.

Поэтому здесь — единственная уборка, которая идёт **от файла к строке**:
что на диске есть, а в БД не значится. Направление обратное остальным, и
цена ошибки в нём тоже обратная: там лишний файл переживает удаление, здесь
ошибка удаляет запись, которая нужна. Отсюда два правила модуля:

* спрашивается не «какие камеры есть» (ответ «ни одной» стёр бы весь
  архив), а «существуют ли **вот эти** камеры, чьи файлы лежат на диске» —
  список идентификаторов всегда конечный и снятый с диска;
* решение принимается только по успешному ответу БД: сбой запроса — это
  исключение у вызывающего, до удаления дело не доходит.

Только stdlib: модуль идёт в лёгкую CI-джобу воркера, которая не ставит
cv2/insightface/onnxruntime (см. .github/workflows/ci.yml).
"""
from __future__ import annotations

import logging
import os

from record_layer import parse_segment_name
from thumbs import THUMB_DIR

logger = logging.getLogger("facewatch.worker.orphan_media")


def segment_camera_ids(segments_dir: str) -> dict[int, list[str]]:
    """Камеры, чьи сегменты лежат в каталоге: {camera_id: [путь, ...]}.

    Имена, не похожие на сегмент (`cam3_1754460000.mp4`), пропускаются
    молча: в каталоге может лежать что угодно чужое, и уборка архива не
    вправе судить о файлах, которых не создавала. Незавершённые
    `*_tmp.mp4` под маску `parse_segment_name` не подходят и остаются
    `fileage.prune_media`, где у них свой короткий срок.
    """
    out: dict[int, list[str]] = {}
    try:
        names = os.listdir(segments_dir)
    except OSError:
        return {}
    for name in names:
        parsed = parse_segment_name(name)
        if parsed is None:
            continue
        cam_id, _ts = parsed
        out.setdefault(cam_id, []).append(os.path.join(segments_dir, name))
    return out


def thumb_segment_ids(media_root: str) -> dict[int, str]:
    """Миниатюры на диске: {segment_id: путь} (см. `thumbs.thumb_rel_path`).

    Раскладка двухуровневая (`thumbs/<id//1000>/<id>.jpg`), поэтому обход
    идёт по подкаталогам, а не одним `listdir`.
    """
    out: dict[int, str] = {}
    root = os.path.join(media_root, THUMB_DIR)
    try:
        buckets = os.listdir(root)
    except OSError:
        return {}
    for bucket in buckets:
        bucket_dir = os.path.join(root, bucket)
        try:
            names = os.listdir(bucket_dir)
        except OSError:
            continue
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext != ".jpg" or not stem.isdigit():
                continue
            out[int(stem)] = os.path.join(bucket_dir, name)
    return out


def remove_files(paths) -> tuple[int, int]:
    """Удаляет перечисленные файлы. Возвращает (удалено, отказов).

    Отказ по одному файлу не прерывает остальные и логируется: каталог,
    переставший чиститься (сменились права после восстановления из
    бэкапа), иначе выглядел бы как «уборка работает» — тот же урок, что в
    `fileage.prune_older_than`.
    """
    removed = failed = 0
    for path in paths:
        try:
            os.remove(path)
            removed += 1
        except FileNotFoundError:
            continue  # удалён параллельно — нормальный исход
        except OSError:
            failed += 1
            logger.warning("не удалось удалить осиротевший файл",
                           extra={"path": path}, exc_info=True)
    return removed, failed
