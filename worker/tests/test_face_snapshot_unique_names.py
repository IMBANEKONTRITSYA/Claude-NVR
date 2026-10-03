"""Два лица одного кадра не получают один и тот же файл снимка.

Находка цикла 20 (P1, целостность данных). `save_face_snapshot()` строила
имя как `cam{id}_{миллисекунды}.jpg`. Все лица одного кадра режутся
подряд — между ними нет ни запроса к БД, ни обращения к сети, — поэтому
два кропа штатно укладываются в одну миллисекунду. Второй `cv2.imwrite`
молча затирал первый, оба события оставались в БД, но `snapshot_path` у
них указывал на один файл: в карточке одного человека, и в его аватаре,
оказывалось лицо другого.

Разные камеры не конфликтовали и раньше (`cam_id` в имени) — конфликтовали
лица внутри одного кадра одной камеры.

Проверка идёт по фактическому поведению функции, а не по формату имени:
режутся два **разных** участка кадра, и тест смотрит, что на диске лежат
два файла и содержимое второго не затёрло первый. Тест на формат имени
прошёл бы и на реализации, которая просто добавила суффикс, но продолжила
перезаписывать.
"""
import os

import pytest

# Все тяжёлые импорты — строго после importorskip (урок цикла 16).
worker = pytest.importorskip(
    "worker",
    reason="нужен полный requirements.txt воркера (cv2/numpy)",
)

import numpy as np  # noqa: E402


@pytest.fixture()
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "MEDIA_PATH", str(tmp_path))
    (tmp_path / "snapshots").mkdir()
    return tmp_path


def test_two_faces_of_one_frame_get_distinct_files(media_dir, monkeypatch):
    """Два кропа в одну миллисекунду — два разных файла, второй не затирает первый.

    Часы замораживаются намеренно. Без этого тест зависел бы от того,
    уложились ли два `cv2.imwrite` в одну миллисекунду на конкретной
    машине: на медленном диске они разъезжаются, и тест проходил бы даже
    на сломанной реализации (проверено откатом — так и было). Замороженное
    время воспроизводит ровно ту ситуацию, которая в проде возникает сама
    собой на кадре с несколькими людьми.
    """
    monkeypatch.setattr(worker.time, "time", lambda: 1_785_000_000.123)

    # Кадр, у которого левая и правая половины заведомо различимы: слева
    # чёрное, справа белое. Если второй кроп затрёт первый, содержимое
    # «левого» файла станет белым.
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    frame[:, 320:] = 255

    left = worker.save_face_snapshot(frame, 7, [10, 100, 110, 200])
    right = worker.save_face_snapshot(frame, 7, [420, 100, 520, 200])

    assert left and right, "оба кропа должны сохраниться"
    assert left != right, (
        f"два лица одного кадра получили один файл ({left}) — "
        "второй кроп затёр первый"
    )

    snap_dir = media_dir / "snapshots"
    files = sorted(p.name for p in snap_dir.iterdir())
    assert len(files) == 2, f"на диске должно быть два файла, а не {files}"

    # И содержимое сохранилось раздельно: «левый» файл всё ещё тёмный.
    import cv2

    left_img = cv2.imread(str(media_dir / left))
    right_img = cv2.imread(str(media_dir / right))
    assert left_img is not None and right_img is not None
    assert left_img.mean() < 64, "левый кроп затёрт правым (стал светлым)"
    assert right_img.mean() > 191, "правый кроп записан неверно"


def test_many_crops_in_same_millisecond_stay_distinct(media_dir, monkeypatch):
    """Пачка кропов подряд — столько же файлов, сколько вызовов.

    Кадр с несколькими людьми — обычная ситуация для проходной, ради
    которой система и ставится; на миллисекундном имени часть снимков
    просто исчезала.
    """
    monkeypatch.setattr(worker.time, "time", lambda: 1_785_000_000.456)
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    paths = [worker.save_face_snapshot(frame, 3, [10, 10, 60, 60]) for _ in range(12)]

    assert all(paths), "все кропы должны сохраниться"
    assert len(set(paths)) == 12, (
        f"уникальных имён {len(set(paths))} из 12 — часть снимков затёрта"
    )
    assert len(list((media_dir / "snapshots").iterdir())) == 12
