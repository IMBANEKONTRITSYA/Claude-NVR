"""Путь миниатюры сегмента архива — копия для воркера (SPEC §7).

Миниатюры режет бэкенд по запросу (`backend/app/services/thumbs.py`), но
**удаляет** их воркер: он один владеет ротацией архива (§5) и один знает,
какой сегмент сейчас уходит с диска. Зависимости от пакета `app` у воркера
нет — по той же причине, по которой он держит собственные копии
ORM-моделей и SMTP-транспорта.

Дублируется только вычисление пути (две строки), решений здесь нет.
Расхождение копий стережёт `backend/tests/test_thumbs_parity.py`: разойдись
они молча — ротация удаляла бы файлы по несуществующим путям, миниатюры
пережили бы свои сегменты, и на объекте это выглядело бы как медленно
растущий диск без единой ошибки в логах.
"""
from __future__ import annotations

import os

THUMB_DIR = "thumbs"


def thumb_rel_path(seg_id: int) -> str:
    """Путь миниатюры относительно MEDIA_PATH."""
    return os.path.join(THUMB_DIR, str(seg_id // 1000), f"{seg_id}.jpg")


def thumb_path(media_root: str, seg_id: int) -> str:
    """Абсолютный путь миниатюры сегмента."""
    return os.path.join(media_root, thumb_rel_path(seg_id))


def drop_thumb(media_root: str, seg_id: int) -> bool:
    """Удалить миниатюру сегмента. `True`, если файл был.

    Отсутствие файла — норма, а не ошибка: миниатюра появляется только у
    сегментов, которые кто-то открывал в выдаче архива, а под ротацию
    попадают все подряд.
    """
    path = thumb_path(media_root, seg_id)
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
