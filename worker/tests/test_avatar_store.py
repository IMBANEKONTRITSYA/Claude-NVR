"""Аватар персоны живёт столько же, сколько карточка (SPEC §15; цикл 53).

Только stdlib — набор идёт и в лёгкой CI-джобе воркера. Production path
(назначение аватара в `process_faces` и уборка осиротевших) — в
`test_orphan_media_live.py` и `test_process_faces_*`.

Разбираемый дефект: `persons.avatar_path` заполнялся двумя путями в
каталоги с разными сроками жизни. Ручная загрузка кладёт файл в
`avatars/`, который уборка по возрасту не трогает намеренно (сторож —
`test_fileage.py::test_prune_media_keeps_avatars`); автоматическое
назначение указывало прямо в `snapshots/`, а он чистится по возрасту.
Через `retention_days` карточка теряла фото навсегда — воркер назначает
аватар только когда его нет, а он «есть», просто ведёт в никуда.
"""
import os

from avatar_store import AVATAR_DIR, adopt_snapshot, avatar_files, needs_avatar


def _snap(root, rel="snapshots/cam1_1700000000_abcd.jpg", data=b"jpeg"):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return rel


# --- needs_avatar ---------------------------------------------------------


def test_a_person_without_an_avatar_needs_one(tmp_path):
    assert needs_avatar(None, str(tmp_path)) is True
    assert needs_avatar("", str(tmp_path)) is True


def test_an_avatar_in_its_own_directory_is_never_questioned(tmp_path):
    """Файла может не быть (свежая БД, восстановленная без медиа), но
    `stat` на каждое событие распознавания — плата ни за что: у аватара в
    `avatars/` срока жизни нет, уборка по возрасту его не трогает."""
    assert needs_avatar("avatars/manual_dead.jpg", str(tmp_path)) is False


def test_a_live_snapshot_reference_is_left_alone(tmp_path):
    """Карточка, заведённая до цикла 53, но чей снимок ещё жив, не
    трогается: перезаписывать рабочий аватар незачем."""
    rel = _snap(str(tmp_path))
    assert needs_avatar(rel, str(tmp_path)) is False


def test_a_snapshot_eaten_by_retention_counts_as_no_avatar(tmp_path):
    """Главное свойство: ссылка есть, файла нет — значит аватара нет.

    Без этого карточка остаётся с битой картинкой навсегда: назначение
    шло по `avatar_path is None`, а он не None.
    """
    assert needs_avatar("snapshots/cam1_gone.jpg", str(tmp_path)) is True


# --- adopt_snapshot -------------------------------------------------------


def test_the_snapshot_is_copied_into_the_avatar_directory(tmp_path):
    rel = _snap(str(tmp_path), data=b"\xff\xd8face")

    adopted = adopt_snapshot(str(tmp_path), rel)

    assert adopted.startswith(AVATAR_DIR + "/")
    with open(os.path.join(str(tmp_path), adopted), "rb") as fh:
        assert fh.read() == b"\xff\xd8face"


def test_the_snapshot_itself_survives_the_copy(tmp_path):
    """Копия, а не перенос: на тот же файл ссылается
    `face_events.snapshot_path`, и унести его из-под события значило бы
    сломать ленту распознавания ради аватара."""
    rel = _snap(str(tmp_path))

    adopt_snapshot(str(tmp_path), rel)

    assert os.path.exists(os.path.join(str(tmp_path), rel))


def test_the_copy_is_not_questioned_afterwards(tmp_path):
    """Свойство, ради которого всё делалось: назначенный аватар больше не
    считается отсутствующим — то есть переназначения на каждом событии не
    будет, и уборка по возрасту до него не дотянется."""
    rel = _snap(str(tmp_path))

    adopted = adopt_snapshot(str(tmp_path), rel)

    assert needs_avatar(adopted, str(tmp_path)) is False


def test_two_persons_seen_in_the_same_millisecond_do_not_share_a_file(tmp_path):
    rel = _snap(str(tmp_path))

    first = adopt_snapshot(str(tmp_path), rel)
    second = adopt_snapshot(str(tmp_path), rel)

    assert first != second


def test_a_missing_snapshot_yields_no_avatar_at_all(tmp_path):
    """`None`, а не ссылка на `snapshots/`: битая ссылка хуже пустой
    карточки — пустую воркер назначит при следующей встрече, битую нет."""
    assert adopt_snapshot(str(tmp_path), "snapshots/never_written.jpg") is None
    assert adopt_snapshot(str(tmp_path), "") is None


# --- avatar_files ---------------------------------------------------------


def test_avatars_are_keyed_the_way_the_database_stores_them(tmp_path):
    """Ключ — ровно та строка, что лежит в `persons.avatar_path`, чтобы
    сверка шла сравнением значений, а не разбором путей на двух сторонах."""
    d = tmp_path / AVATAR_DIR
    d.mkdir()
    (d / "manual_dead.jpg").write_bytes(b"x")
    (d / "auto_beef.jpg").write_bytes(b"x")
    (d / "subdir").mkdir()

    found = avatar_files(str(tmp_path))

    assert sorted(found) == ["avatars/auto_beef.jpg", "avatars/manual_dead.jpg"]
    assert found["avatars/auto_beef.jpg"] == str(d / "auto_beef.jpg")


def test_missing_avatar_directory_is_not_an_error(tmp_path):
    assert avatar_files(str(tmp_path)) == {}
