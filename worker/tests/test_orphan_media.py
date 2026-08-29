"""Решения уборки осиротевших медиа-файлов (SPEC §3, §5; цикл 53).

Только stdlib, поэтому набор выполняется **везде**, включая лёгкую
CI-джобу воркера без cv2/insightface. Production path — настоящие файлы,
настоящая БД и настоящая `worker.prune_orphan_media()` — лежит в
`test_orphan_media_live.py`: там `pytest.importorskip("worker")`, а он
скипает модуль целиком, вместе с чистыми тестами, если они в том же
файле. Ровно этим первая редакция набора и обманула бы лёгкую джобу.

Зачем модуль нужен, объяснено в шапке `orphan_media.py`: удаление камеры
через веб-интерфейс (§3) уносит строки архива каскадом Postgres, мимо
воркера, и файлы остаётся некому выбрать — retention и циклическая
перезапись идут по строкам.
"""
import os

import pytest

from orphan_media import remove_files, segment_camera_ids, thumb_segment_ids


# --- Решения: только stdlib, выполняются везде ----------------------------


def test_segment_files_are_grouped_by_camera(tmp_path):
    d = tmp_path / "segments"
    d.mkdir()
    for name in ("cam1_1000.mp4", "cam1_1300.mp4", "cam12_1000.mp4"):
        (d / name).write_bytes(b"x")

    found = segment_camera_ids(str(d))

    assert sorted(found) == [1, 12]
    assert sorted(os.path.basename(p) for p in found[1]) == ["cam1_1000.mp4", "cam1_1300.mp4"]


def test_foreign_files_are_left_alone(tmp_path):
    """Каталог архива — не собственность уборки: что не похоже на сегмент,
    её не касается. `*_tmp.mp4` — незавершённая запись, у неё свой короткий
    срок в `fileage.prune_media`, и попасть под удаление по «камеры нет»
    она не должна: файл может писаться прямо сейчас."""
    d = tmp_path / "segments"
    d.mkdir()
    for name in ("cam1_1000.mp4", "cam1_1300_tmp.mp4", "notes.txt", "cam.mp4",
                 "camX_1.mp4"):
        (d / name).write_bytes(b"x")

    found = segment_camera_ids(str(d))

    assert sorted(found) == [1]
    assert [os.path.basename(p) for p in found[1]] == ["cam1_1000.mp4"]


def test_missing_segments_dir_is_not_an_error(tmp_path):
    assert segment_camera_ids(str(tmp_path / "nope")) == {}
    assert thumb_segment_ids(str(tmp_path / "nope")) == {}


def test_thumbs_are_found_across_buckets(tmp_path):
    """Раскладка миниатюр двухуровневая: `thumbs/<id//1000>/<id>.jpg`."""
    for seg_id in (7, 1500, 2001):
        p = tmp_path / "thumbs" / str(seg_id // 1000)
        p.mkdir(parents=True, exist_ok=True)
        (p / f"{seg_id}.jpg").write_bytes(b"x")
    (tmp_path / "thumbs" / "0" / "readme.txt").write_bytes(b"x")

    found = thumb_segment_ids(str(tmp_path))

    assert sorted(found) == [7, 1500, 2001]
    assert found[1500].endswith("thumbs/1/1500.jpg")


def test_remove_files_counts_and_survives_a_missing_file(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(b"x")
    b.write_bytes(b"x")

    removed, failed = remove_files([str(a), str(tmp_path / "gone"), str(b)])

    assert (removed, failed) == (2, 0)
    assert not a.exists() and not b.exists()


def test_remove_files_reports_a_refusal_instead_of_hiding_it(tmp_path):
    """Каталог вместо файла: `os.remove` отвечает OSError. Отказ обязан
    попасть в счётчик, иначе переставшая работать уборка (сменились права
    после восстановления из бэкапа) выглядит как успешная."""
    d = tmp_path / "subdir"
    d.mkdir()

    removed, failed = remove_files([str(d)])

    assert (removed, failed) == (0, 1)
