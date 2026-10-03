"""«Запись только при движении» на настоящих строках БД (SPEC §6).

`test_motion_windows.py` проверяет решения (какое окно закрыть, какой
сегмент считать пустым); здесь — что эти решения доезжают до диска и до
таблиц: настоящие `cameras`, `video_segments`, `motion_windows`,
настоящая `prune_motionless_segments()` воркера с её запросами.

Разрыв, который закрывает этот файл, тот же, что и у ротации архива:
чистая логика может быть верной, а функция — нерабочей, потому что
ошибка сидит в отборе кандидатов (окна тянутся за неверный промежуток,
камера не в том режиме, сегмент моложе settle). Ни один тест над
списками этого не увидит.

БД — SQLite по умолчанию (как в `test_retention_rotation.py`), настоящий
Postgres подключается `SEGMENT_INDEX_TEST_DATABASE_URL`.
"""
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

from sqlalchemy import create_engine, text as _sql  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from motion_windows import MAX_WINDOW_SEC, SETTLE_SEC  # noqa: E402

NOW = datetime(2026, 8, 17, 12, 0, 0)
# Возраст НАЧАЛА сегмента, при котором он уже подлежит суду. Берётся от
# SETTLE_SEC, а не константой: разъедься они — тест начал бы проверять не
# тот сценарий, молча и в зелёном виде. Запас поверх settle — длина самого
# сегмента: отбор смотрит на `ended_at`, и первая редакция теста с
# `SETTLE_SEC * 2` попадала ровно в границу `<` и не отбирала ничего.
OLD_ENOUGH = timedelta(seconds=SETTLE_SEC * 2 + 600)


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    url = os.environ.get("SEGMENT_INDEX_TEST_DATABASE_URL")
    url = url.replace("+asyncpg", "") if url else f"sqlite:///{tmp_path / 'motion.db'}"
    is_pg = not url.startswith("sqlite")

    # Отдельная схема Postgres по той же причине, что и в ротации: имена
    # таблиц совпадают с бэкендовскими, и create/drop на общей БД снёс бы
    # чужую схему.
    schema = "motion_test"
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"} if is_pg else {},
    )
    if is_pg:
        with create_engine(url).begin() as admin:
            admin.execute(_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            admin.execute(_sql(f"CREATE SCHEMA {schema}"))

    tables = [worker.Camera.__table__, worker.VideoSegment.__table__,
              worker.MotionWindow.__table__]
    for t in tables:
        t.drop(engine, checkfirst=True)
        t.create(engine, checkfirst=True)

    Session = sessionmaker(bind=engine)
    segdir = tmp_path / "segments"
    segdir.mkdir()
    monkeypatch.setattr(worker, "Session", Session)
    monkeypatch.setattr(worker, "MEDIA_PATH", str(tmp_path))

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return NOW

    monkeypatch.setattr(worker, "datetime", _DT)

    class _Helper:
        def __init__(self):
            # Присваивание в теле класса (`Session = Session`) не видит
            # локальную переменной фикстуры — область видимости тела класса
            # не замыкает функцию.
            self.Session = Session

        def camera(self, cam_id, *, mode="analytics", record_on_motion=True):
            with Session() as s:
                s.add(worker.Camera(id=cam_id, name=f"cam{cam_id}", enabled=True,
                                    mode=mode, status="online",
                                    record_on_motion=record_on_motion))
                s.commit()

        def segment(self, cam_id, *, start, minutes=5, size=4096):
            path = segdir / f"cam{cam_id}_{int(start.timestamp())}.mp4"
            path.write_bytes(b"\0" * size)
            with Session() as s:
                s.add(worker.VideoSegment(
                    camera_id=cam_id, started_at=start,
                    ended_at=start + timedelta(minutes=minutes),
                    file_path=str(path), event_type="continuous",
                    duration_sec=minutes * 60, size_bytes=size))
                s.commit()
            return path

        def cover(self, cam_id, start, end, *, motion=False):
            """Заполнить промежуток окнами наблюдения по минуте, как воркер."""
            with Session() as s:
                cur = start
                while cur < end:
                    nxt = min(cur + timedelta(seconds=MAX_WINDOW_SEC), end)
                    s.add(worker.MotionWindow(camera_id=cam_id, started_at=cur,
                                              ended_at=nxt, motion=motion))
                    cur = nxt
                s.commit()

        def files(self):
            return {p.name for p in segdir.iterdir()}

        def rows(self):
            with Session() as s:
                return {os.path.basename(r.file_path)
                        for r in s.query(worker.VideoSegment).all()}

    yield _Helper()

    if is_pg:
        engine.dispose()
        with create_engine(url).begin() as admin:
            admin.execute(_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    else:
        for t in tables:
            t.drop(engine, checkfirst=True)
    engine.dispose()


def _quiet_segment(archive, cam_id=1, *, age=None):
    """Сегмент, полностью просмотренный аналитикой без движения."""
    start = NOW - (age or OLD_ENOUGH)
    path = archive.segment(cam_id, start=start)
    archive.cover(cam_id, start - timedelta(minutes=1),
                  start + timedelta(minutes=6), motion=False)
    return path


def test_quiet_segment_is_removed_from_db_and_disk(archive):
    archive.camera(1)
    path = _quiet_segment(archive)

    assert worker.prune_motionless_segments() == 1

    assert archive.rows() == set()
    assert not path.exists(), "файл сегмента без движения должен быть удалён"


def test_segment_with_motion_stays(archive):
    """Позитивный контроль: механизм сносит не всё подряд."""
    archive.camera(1)
    start = NOW - OLD_ENOUGH
    path = archive.segment(1, start=start)
    archive.cover(1, start - timedelta(minutes=1), start + timedelta(minutes=2),
                  motion=False)
    archive.cover(1, start + timedelta(minutes=2), start + timedelta(minutes=6),
                  motion=True)

    assert worker.prune_motionless_segments() == 0
    assert archive.rows() == {path.name} and path.exists()


def test_option_disabled_keeps_everything(archive):
    """Флаг выключен — архив не трогается, сколько бы окон ни было."""
    archive.camera(1, record_on_motion=False)
    path = _quiet_segment(archive)

    assert worker.prune_motionless_segments() == 0
    assert archive.rows() == {path.name}


def test_record_only_camera_is_never_pruned(archive):
    """Камера вне режима analytics не судится даже с поднятым флагом.

    Такую камеру не декодирует никто, окон у неё быть не может, но
    страховка стоит и в запросе: включить флаг можно было импортом файла,
    сделанного до проверки в API.
    """
    archive.camera(1, mode="record_only", record_on_motion=True)
    path = _quiet_segment(archive)

    assert worker.prune_motionless_segments() == 0
    assert archive.rows() == {path.name}


def test_unobserved_segment_survives(archive):
    """Аналитика не работала — сегмент остаётся.

    Тот самый сценарий, ради которого пишутся окна наблюдения, а не
    отметки о движении: упавший воркер не должен стирать архив.
    """
    archive.camera(1)
    path = archive.segment(1, start=NOW - OLD_ENOUGH)

    assert worker.prune_motionless_segments() == 0
    assert archive.rows() == {path.name} and path.exists()


def test_fresh_segment_is_not_judged_yet(archive):
    """Сегмент моложе settle не трогается: его окна ещё не дописаны.

    Возраст считается по КОНЦУ сегмента, поэтому сценарий строится
    вручную: сегмент дописан минуту назад (то есть в отбор по времени
    попал бы), но settle ещё не вышел. Первая редакция теста брала
    «начался SETTLE/2 назад», из-за чего сегмент заканчивался в будущем и
    отбор пропускал его по другой причине — снятие проверки settle такой
    тест не ловил (мутация M8 проходила зелёной).
    """
    archive.camera(1)
    start = NOW - timedelta(minutes=6)
    path = archive.segment(1, start=start)          # закончился минуту назад
    archive.cover(1, start - timedelta(minutes=1), start + timedelta(minutes=6))
    assert NOW - path_end(start) < timedelta(seconds=SETTLE_SEC)

    assert worker.prune_motionless_segments() == 0
    assert archive.rows() == {path.name}


def path_end(start, minutes=5):
    return start + timedelta(minutes=minutes)


def test_neighbour_camera_archive_untouched(archive):
    """Соседняя камера без режима не страдает от уборки первой."""
    archive.camera(1)
    archive.camera(2, record_on_motion=False)
    quiet = _quiet_segment(archive, 1)
    neighbour = _quiet_segment(archive, 2)

    assert worker.prune_motionless_segments() == 1
    assert archive.rows() == {neighbour.name}
    assert not quiet.exists() and neighbour.exists()


def test_motion_windows_expire_with_archive(archive, monkeypatch):
    """Окна наблюдения не переживают глубину хранения (SPEC §5).

    Иначе таблица растёт вечно: 1440 строк в сутки на камеру аналитики.
    """
    monkeypatch.setitem(worker.CONFIG, "retention_days", 7)
    archive.camera(1)
    # По минуте на окно, как их пишет воркер: пять старых и пять свежих.
    archive.cover(1, NOW - timedelta(days=10), NOW - timedelta(days=10) + timedelta(minutes=5))
    archive.cover(1, NOW - timedelta(hours=1), NOW - timedelta(minutes=55))
    with archive.Session() as s:
        assert s.query(worker.MotionWindow).count() == 10

    # Каталоги, которые обходит prune_media внутри cleanup_old().
    for sub in ("snapshots", "avatars"):
        os.makedirs(os.path.join(worker.MEDIA_PATH, sub), exist_ok=True)
    with archive.Session() as s:
        s.execute(_sql("CREATE TABLE IF NOT EXISTS face_events (id INTEGER PRIMARY KEY, ts TIMESTAMP)"))
        s.commit()

    worker.cleanup_old()

    with archive.Session() as s:
        left = s.query(worker.MotionWindow).all()
    assert len(left) == 5, "старые окна должны уйти, свежие остаться"
    assert all(w.started_at > NOW - timedelta(days=1) for w in left)
