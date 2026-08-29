"""Возраст файлов при уборке считается в UTC, а не в поясе хоста.

Находка цикла 20 в `worker.py:cleanup_old()`: порог удаления считался от
`datetime.utcnow()`, а возраст файла — `datetime.fromtimestamp(mtime)` без
`tz`, то есть в **локальном** времени хоста. Сравнение уезжало ровно на
смещение часового пояса. Класс ошибок повторяющийся — циклы 3 и 7 находили
то же расхождение naive/aware в других местах (см. docs/reviews/REVIEW_LOG.md).

Тесты подставляют `TZ` через `time.tzset()`, потому что иначе на UTC-хосте
(так идут дефолтные контейнеры проекта) ошибка не проявляется вовсе — ровно
поэтому она и дожила до 20-го цикла.

Только stdlib: файл идёт в CI-джоб воркера, который не ставит
cv2/insightface/onnxruntime.
"""
import os
import time
from datetime import datetime, timedelta, timezone

import pytest

from fileage import mtime_utc, prune_media, prune_older_than

# Пояса по обе стороны от UTC: смещения ломают сравнение в разные стороны —
# восточнее файл кажется новее (уборка не наступает), западнее старее
# (удаляется то, что ещё пишется).
TIMEZONES = ["Asia/Yekaterinburg", "America/New_York", "UTC"]


@pytest.fixture()
def in_timezone():
    """Подменяет часовой пояс процесса и возвращает его обратно."""
    saved = os.environ.get("TZ")

    def _set(tz: str):
        os.environ["TZ"] = tz
        time.tzset()

    yield _set
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def _touch(path, *, age_seconds: float = 0.0):
    path.write_bytes(b"x")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


@pytest.mark.parametrize("tz", TIMEZONES)
def test_mtime_utc_matches_utcnow_in_any_timezone(tz, in_timezone, tmp_path):
    """Файл, изменённый только что, в любом поясе должен читаться как «сейчас»
    по UTC. С `datetime.fromtimestamp(mtime)` без tz расхождение здесь равно
    смещению пояса — 5 часов для обоих ненулевых случаев."""
    in_timezone(tz)
    f = _touch(tmp_path / "now.jpg")

    delta = abs((mtime_utc(str(f)) - datetime.utcnow()).total_seconds())
    assert delta < 60, f"возраст файла разошёлся с UTC на {delta:.0f} с в поясе {tz}"


@pytest.mark.parametrize("tz", TIMEZONES)
def test_file_being_written_now_is_not_pruned_by_one_hour_window(tz, in_timezone, tmp_path):
    """Ключевой сценарий уборки осиротевших сегментов: `*_tmp.mp4`, в который
    прямо сейчас пишет ffmpeg, не должен попасть под окно «старше часа».

    В поясе западнее UTC (America/New_York, −05:00) старый код считал такой
    файл на 5 часов старше, чем он есть, и удалял запись из-под пишущего
    процесса."""
    in_timezone(tz)
    live = _touch(tmp_path / "cam1_1700000000_tmp.mp4")
    stale = _touch(tmp_path / "cam2_1600000000_tmp.mp4", age_seconds=3 * 3600)

    removed = prune_older_than(
        str(tmp_path), datetime.utcnow() - timedelta(hours=1),
        select=lambda name: name.endswith("_tmp.mp4"),
    )

    assert removed == 1
    assert live.exists(), f"удалён файл, в который ещё идёт запись (пояс {tz})"
    assert not stale.exists(), f"осиротевший файл не удалён (пояс {tz})"


@pytest.mark.parametrize("tz", TIMEZONES)
def test_expired_file_is_pruned_in_any_timezone(tz, in_timezone, tmp_path):
    """Обратная сторона: в поясе восточнее UTC (Asia/Yekaterinburg, +05:00)
    старый код считал файлы новее, чем они есть, и окно уборки наступало на
    смещение пояса позже — здесь файл старше окна ровно на 2 часа, чего для
    +05:00 не хватало."""
    in_timezone(tz)
    old = _touch(tmp_path / "cam1_1600000000_tmp.mp4", age_seconds=3 * 3600)

    removed = prune_older_than(
        str(tmp_path), datetime.utcnow() - timedelta(hours=1),
        select=lambda name: name.endswith("_tmp.mp4"),
    )

    assert removed == 1
    assert not old.exists(), f"файл старше окна не удалён в поясе {tz}"


def test_select_filter_keeps_files_that_do_not_match(tmp_path):
    """Снимок текущего кадра Стены (`cam{id}_latest.jpg`) перезаписывается на
    месте и не является историей — retention его не касается, каким бы старым
    ни был mtime."""
    latest = _touch(tmp_path / "cam1_latest.jpg", age_seconds=90 * 86400)
    history = _touch(tmp_path / "cam1_1600000000.jpg", age_seconds=90 * 86400)

    removed = prune_older_than(
        str(tmp_path), datetime.utcnow() - timedelta(days=14),
        select=lambda name: not name.endswith("_latest.jpg"),
    )

    assert removed == 1
    assert latest.exists()
    assert not history.exists()


def test_missing_directory_is_not_an_error(tmp_path):
    assert prune_older_than(str(tmp_path / "no-such-dir"), datetime.utcnow()) == 0


def test_subdirectories_are_left_alone(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    old = time.time() - 90 * 86400
    os.utime(nested, (old, old))

    assert prune_older_than(str(tmp_path), datetime.utcnow()) == 0
    assert nested.is_dir()


def test_file_removed_concurrently_does_not_abort_the_sweep(tmp_path, monkeypatch):
    """Гонка с параллельным удалением не должна прерывать обход остальных
    файлов: иначе один исчезнувший файл оставлял бы каталог неубранным."""
    for i in range(3):
        _touch(tmp_path / f"old{i}.jpg", age_seconds=86400)

    real_remove = os.remove
    calls = {"n": 0}

    def flaky_remove(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise FileNotFoundError(path)  # как будто удалён параллельно
        return real_remove(path)

    monkeypatch.setattr(os, "remove", flaky_remove)
    removed = prune_older_than(str(tmp_path), datetime.utcnow())

    assert calls["n"] == 3, "обход прервался на первой ошибке"
    assert removed == 2


def _media_tree(root):
    for sub in ("snapshots", "segments", "avatars", "uploads"):
        (root / sub).mkdir()
    return root


def test_search_uploads_are_covered_by_retention(tmp_path):
    """Главная находка цикла 20 в этом файле: каталог `uploads` не убирал
    никто, и он рос без границы всё время работы системы.

    Проверка идёт по фактическому дереву MEDIA_PATH, а не по вызову
    `prune_older_than` напрямую: пропуск каталога — это именно то, что
    отсутствие вызова и означало.
    """
    media = _media_tree(tmp_path)
    old_upload = _touch(media / "uploads" / "abc123_photo.jpg", age_seconds=20 * 86400)
    fresh_upload = _touch(media / "uploads" / "def456_photo.jpg", age_seconds=3600)

    removed = prune_media(
        str(media),
        cutoff=datetime.utcnow() - timedelta(days=14),
        tmp_cutoff=datetime.utcnow() - timedelta(hours=1),
    )

    assert removed["uploads"] == 1, "загруженные для поиска фото не попадают под retention"
    assert not old_upload.exists()
    assert fresh_upload.exists(), "удалено фото, которое ещё в пределах retention"


def test_prune_media_covers_every_directory_that_grows(tmp_path):
    """Состав уборки фиксируется тестом: каталог, который начнёт расти, но не
    попадёт в `prune_media`, снова окажется без retention незамеченным."""
    media = _media_tree(tmp_path)
    removed = prune_media(str(media), datetime.utcnow(), datetime.utcnow())
    assert set(removed) == {"snapshots", "uploads", "segments_tmp"}


def test_prune_media_keeps_avatars(tmp_path):
    """Аватары персон удалять по возрасту нельзя: на них ссылается
    `persons.avatar_path`, и файл активной персоны старше retention должен
    остаться (иначе карточка потеряла бы фото)."""
    media = _media_tree(tmp_path)
    avatar = _touch(media / "avatars" / "manual_deadbeef.jpg", age_seconds=365 * 86400)

    prune_media(str(media), datetime.utcnow(), datetime.utcnow())

    assert avatar.exists()


def test_prune_media_applies_short_window_only_to_tmp_segments(tmp_path):
    """Готовые сегменты архива удаляет БД-часть `cleanup_old()` вместе с их
    строками, а не файловая уборка: часовое окно применимо только к
    `*_tmp.mp4`, иначе снесло бы весь свежий архив."""
    media = _media_tree(tmp_path)
    ready = _touch(media / "segments" / "cam1_1700000000.mp4", age_seconds=3 * 3600)
    orphan = _touch(media / "segments" / "cam1_1700000000_tmp.mp4", age_seconds=3 * 3600)

    removed = prune_media(
        str(media),
        cutoff=datetime.utcnow() - timedelta(days=14),
        tmp_cutoff=datetime.utcnow() - timedelta(hours=1),
    )

    assert removed["segments_tmp"] == 1
    assert ready.exists(), "удалён готовый сегмент архива вместо временного"
    assert not orphan.exists()


def test_mtime_utc_returns_naive_datetime():
    """Значение сравнивается с `datetime.utcnow()` и с метками из колонок
    `TIMESTAMP WITHOUT TIME ZONE` — tzinfo здесь сломал бы сравнение
    `TypeError`ом."""
    assert mtime_utc(__file__).tzinfo is None
    # И оно действительно про UTC, а не про локальное время.
    aware = datetime.fromtimestamp(os.path.getmtime(__file__), tz=timezone.utc)
    assert mtime_utc(__file__) == aware.replace(tzinfo=None)
