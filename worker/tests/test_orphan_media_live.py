"""Уборка осиротевших медиа-файлов — production path (SPEC §3, §5; цикл 53).

Настоящие файлы на диске, настоящие таблицы, настоящая
`worker.prune_orphan_media()`. Решения того же механизма проверяет
`test_orphan_media.py` — он на одном stdlib и идёт в лёгкой CI-джобе;
здесь нужен полный requirements.txt воркера, и там этот файл пропускается
целиком.

Состояние, ради которого всё написано, создаётся **настоящим каскадом**:
`drop_camera()` делает один DELETE по `cameras`, как это делает
`routers/cameras.delete_camera`, и проверяет, что строки архива после
него исчезли. На SQLite для этого включается `PRAGMA foreign_keys=ON`:
без него каскад пришлось бы изображать вторым DELETE, то есть проверять
имитацию вместо того состояния, которое возникает на объекте.
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

from datetime import datetime, timedelta  # noqa: E402

from sqlalchemy import create_engine, event, select, text as _sql  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    """Настоящие таблицы и каталоги, подставленные воркеру.

    Изоляция та же, что в `test_retention_rotation.py`: на общей БД имена
    таблиц воркера совпадают с бэкендовыми, поэтому Postgres уводится в
    отдельную схему.
    """
    url = os.environ.get("SEGMENT_INDEX_TEST_DATABASE_URL")
    url = url.replace("+asyncpg", "") if url else f"sqlite:///{tmp_path / 'arch.db'}"
    is_pg = not url.startswith("sqlite")

    schema = "orphan_test"
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"} if is_pg else {},
    )
    if not is_pg:
        # Без этого SQLite каскад не исполняет, и тест пришлось бы
        # изображать двумя DELETE — то есть проверять не то состояние,
        # которое в бою создаёт Postgres, а его имитацию.
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _rec):  # pragma: no cover - тривиальный хук
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    if is_pg:
        with create_engine(url).begin() as admin:
            admin.execute(_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            admin.execute(_sql(f"CREATE SCHEMA {schema}"))

    for t in (worker.Camera.__table__, worker.VideoSegment.__table__):
        t.drop(engine, checkfirst=True)
        t.create(engine, checkfirst=True)

    # `persons` заводится минимальным DDL, а не из модели: у неё колонки
    # `Vector(512)` и `ARRAY(Text)`, которых на SQLite нет вовсе. Уборке
    # нужен один столбец `avatar_path` — тот же приём, что применён к
    # `face_events` в `test_retention_rotation.py`.
    with engine.begin() as conn:
        conn.execute(_sql("DROP TABLE IF EXISTS persons"))
        conn.execute(_sql(
            "CREATE TABLE persons (id INTEGER PRIMARY KEY, name VARCHAR, "
            "status VARCHAR, avatar_path VARCHAR)"
            if not is_pg else
            "CREATE TABLE persons (id SERIAL PRIMARY KEY, name VARCHAR, "
            "status VARCHAR, avatar_path VARCHAR)"))

    Session = sessionmaker(bind=engine)
    segdir = tmp_path / "segments"
    segdir.mkdir()
    monkeypatch.setattr(worker, "Session", Session)
    monkeypatch.setattr(worker, "MEDIA_PATH", str(tmp_path))

    class _Helper:
        def __init__(self):
            self.Session = Session
            self.dir = segdir
            self.root = tmp_path

        def camera(self, cam_id):
            with Session() as s:
                s.add(worker.Camera(id=cam_id, name=f"cam{cam_id}", enabled=True,
                                    mode="record_only", status="online"))
                s.commit()

        def segment(self, cam_id, ts, size=4096):
            """Файл + строка архива, как их оставляет индексация."""
            path = segdir / f"cam{cam_id}_{ts}.mp4"
            path.write_bytes(b"\0" * size)
            started = datetime(2026, 8, 1) + timedelta(seconds=ts)
            with Session() as s:
                seg = worker.VideoSegment(
                    camera_id=cam_id, started_at=started,
                    ended_at=started + timedelta(minutes=5),
                    file_path=str(path), event_type="continuous",
                    duration_sec=300, size_bytes=size)
                s.add(seg)
                s.commit()
                return seg.id

        def thumb(self, seg_id):
            p = tmp_path / "thumbs" / str(seg_id // 1000)
            p.mkdir(parents=True, exist_ok=True)
            f = p / f"{seg_id}.jpg"
            f.write_bytes(b"\0" * 128)
            return f

        def person(self, name, avatar_rel=None):
            """Карточка персоны с файлом аватара (SPEC §15)."""
            if avatar_rel:
                f = tmp_path / avatar_rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"\0" * 64)
            with Session() as s:
                pid = s.execute(_sql(
                    "INSERT INTO persons (name, status, avatar_path) "
                    "VALUES (:n, 'known', :a) RETURNING id"),
                    {"n": name, "a": avatar_rel}).scalar()
                s.commit()
                return pid

        def drop_person(self, pid):
            """`DELETE /api/persons/{pid}`: снимает карточку, файл остаётся."""
            with Session() as s:
                s.execute(_sql("DELETE FROM persons WHERE id = :i"), {"i": pid})
                s.commit()

        def drop_camera(self, cam_id):
            """Удаление камеры через веб-интерфейс (SPEC §3).

            Один DELETE по `cameras`, как его делает
            `routers/cameras.delete_camera`: строки архива уносит каскад
            самой БД, и это ровно то состояние, ради которого написан
            модуль, — файлы без строк.
            """
            with Session() as s:
                s.delete(s.get(worker.Camera, cam_id))
                s.commit()
            with Session() as s:
                left = s.execute(select(worker.VideoSegment).where(
                    worker.VideoSegment.camera_id == cam_id)).scalars().all()
            assert left == [], "каскад не сработал — тест проверяет не то состояние"

        def segment_rows(self):
            with Session() as s:
                return s.execute(select(worker.VideoSegment)).scalars().all()

    return _Helper()


def test_segments_of_a_deleted_camera_are_removed_from_disk(archive):
    """Главное: ~21.6 ГБ на камеру в сутки (§16) иначе остаются навсегда.

    Место они занимают настоящее, а видны системе не были: подсчёт архива
    идёт по строкам, которых нет, — то есть циклическая перезапись начала
    бы резать записи живых камер, освобождая место под архив камеры,
    которой уже нет.
    """
    archive.camera(1)
    archive.camera(2)
    archive.segment(1, 1000)
    archive.segment(1, 1300)
    archive.segment(2, 1000)

    archive.drop_camera(1)
    assert len(os.listdir(archive.dir)) == 3, "файлы переживают каскад"

    stats = worker.prune_orphan_media()

    assert stats["segments"] == 2 and stats["failed"] == 0
    assert sorted(os.listdir(archive.dir)) == ["cam2_1000.mp4"]


def test_live_cameras_keep_their_archive(archive):
    """Обратная сторона: уборка идёт от файла к строке, и ошибка в ней
    удаляет запись, которая нужна. На объекте без единого удаления камеры
    она обязана не делать ничего."""
    archive.camera(1)
    archive.segment(1, 1000)
    archive.segment(1, 1300)

    stats = worker.prune_orphan_media()

    assert stats == {"segments": 0, "thumbs": 0, "avatars": 0, "failed": 0}
    assert len(os.listdir(archive.dir)) == 2
    assert len(archive.segment_rows()) == 2


def test_thumbnails_outliving_their_segment_are_removed(archive):
    """Миниатюры (§7) именуются идентификатором сегмента и живут отдельным
    файлом; `_drop_segments` удаляет их по строке, а после каскада строк
    нет. Шапка `thumbs.py` этот исход предсказывала дословно: «миниатюры
    пережили бы свои сегменты, и на объекте это выглядело бы как медленно
    растущий диск без единой ошибки в логах»."""
    archive.camera(1)
    archive.camera(2)
    doomed = archive.segment(1, 1000)
    kept = archive.segment(2, 1000)
    doomed_thumb = archive.thumb(doomed)
    kept_thumb = archive.thumb(kept)

    archive.drop_camera(1)
    assert doomed_thumb.exists()

    stats = worker.prune_orphan_media()

    assert stats["thumbs"] == 1
    assert not doomed_thumb.exists()
    assert kept_thumb.exists()


def test_a_thumb_of_a_rotated_out_segment_is_removed_too(archive):
    """Сиротство миниатюры возможно и без удаления камеры: падение процесса
    между `drop_thumb` и удалением строки. Уборка спрашивает про строку
    сегмента, а не про камеру, поэтому закрывает и этот случай."""
    archive.camera(1)
    seg_id = archive.segment(1, 1000)
    thumb = archive.thumb(seg_id)
    with archive.Session() as s:
        s.execute(_sql("DELETE FROM video_segments WHERE id = :i"), {"i": seg_id})
        s.commit()

    worker.prune_orphan_media()

    assert not thumb.exists()


def test_nothing_to_do_costs_no_deletions(archive):
    """Пустой архив — не повод удалять что-либо и не повод падать."""
    assert worker.prune_orphan_media() == {"segments": 0, "thumbs": 0, "avatars": 0, "failed": 0}


def test_the_avatar_of_a_deleted_person_is_removed(archive):
    """`DELETE /api/persons/{pid}` снимает карточку, а файл в `avatars/` не
    убирал никто: уборка по возрасту этот каталог не трогает намеренно —
    аватар живёт столько же, сколько карточка (см. `avatar_store.py` и
    сторож `test_fileage.py::test_prune_media_keeps_avatars`)."""
    doomed = archive.person("Ушедший", "avatars/auto_dead.jpg")
    archive.person("Оставшийся", "avatars/manual_beef.jpg")
    archive.drop_person(doomed)

    stats = worker.prune_orphan_media()

    assert stats["avatars"] == 1
    assert not (archive.root / "avatars" / "auto_dead.jpg").exists()
    assert (archive.root / "avatars" / "manual_beef.jpg").exists()


def test_a_replaced_avatar_does_not_linger(archive):
    """То же правило само собой закрывает второй случай: карточке
    назначили другой аватар, прежний файл остался. Спрашивается ссылка, а
    не персона, поэтому отдельного кода на это не нужно."""
    pid = archive.person("Обновлённый", "avatars/auto_old.jpg")
    (archive.root / "avatars" / "auto_new.jpg").write_bytes(b"\0" * 64)
    with archive.Session() as s:
        s.execute(_sql("UPDATE persons SET avatar_path = :a WHERE id = :i"),
                  {"a": "avatars/auto_new.jpg", "i": pid})
        s.commit()

    worker.prune_orphan_media()

    assert not (archive.root / "avatars" / "auto_old.jpg").exists()
    assert (archive.root / "avatars" / "auto_new.jpg").exists()


def test_avatars_in_use_are_never_touched(archive):
    """Цена ошибки обратной уборки: на объекте без единого удаления она
    обязана не делать ничего."""
    archive.person("Живой", "avatars/manual_alive.jpg")

    stats = worker.prune_orphan_media()

    assert stats == {"segments": 0, "thumbs": 0, "avatars": 0, "failed": 0}
    assert (archive.root / "avatars" / "manual_alive.jpg").exists()
