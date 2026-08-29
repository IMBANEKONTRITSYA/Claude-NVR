"""Ротация архива и циклическая перезапись — production path (SPEC §5, §21).

`test_storage.py` покрывает решения (какие сегменты просрочены, какие
снести под перезапись); здесь проверяется, что эти решения доезжают до
диска и до БД: настоящие файлы, настоящая таблица `video_segments`,
настоящие `cleanup_old()` и `enforce_disk_quota()` воркера.

Это тот самый разрыв, из-за которого чистая логика может быть верной, а
функция — нерабочей: запрос-отбор кандидатов в `cleanup_old()` идёт по
**минимальному** сроку из действующих, и ошибка в нём не видна ни одному
тесту над списками.

БД — SQLite по умолчанию (как в `test_segment_index.py`), чтобы проверка
выполнялась и в CI-джобе воркера без сервиса Postgres. Настоящий Postgres
подключается `SEGMENT_INDEX_TEST_DATABASE_URL`; проверено против
Postgres 16 в цикле 25.

Требует полный requirements.txt воркера для импорта `worker.py` (урок
цикла 16: тяжёлые импорты строго после `importorskip`).
"""
import os
from datetime import datetime, timedelta

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

NOW = datetime(2026, 8, 6, 12, 0, 0)


@pytest.fixture()
def archive(tmp_path, monkeypatch):
    """Настоящая БД + настоящий каталог сегментов, подставленные воркеру.

    Таблицы создаются только те, что нужны ротации: `Base.metadata` воркера
    целиком тянет pgvector-колонки, которых в SQLite нет.
    """
    from sqlalchemy import text as _sql

    url = os.environ.get("SEGMENT_INDEX_TEST_DATABASE_URL")
    url = url.replace("+asyncpg", "") if url else f"sqlite:///{tmp_path / 'arch.db'}"
    is_pg = not url.startswith("sqlite")

    # Модели воркера несут те же имена таблиц, что и модели бэкенда
    # (`cameras`, `video_segments`, `face_events`), поэтому на общей БД
    # create/drop здесь снёс бы схему бэкенда — ровно то кросс-сервисное
    # загрязнение, на котором цикл 21 потерял прогон, а `cameras` вдобавок
    # не дропается из-за внешних ключей. Изоляция — отдельная схема
    # Postgres с `search_path`: имена таблиц те же, объекты чужие.
    schema = "rotation_test"
    engine = create_engine(
        url,
        connect_args={"options": f"-csearch_path={schema}"} if is_pg else {},
    )
    if is_pg:
        with create_engine(url).begin() as admin:
            admin.execute(_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            admin.execute(_sql(f"CREATE SCHEMA {schema}"))

    # MotionWindow — с цикла 36: `cleanup_old()` чистит и окна наблюдения
    # (SPEC §6), и без таблицы ротация падает на UndefinedTable. На
    # развёртывании таблицу создаёт бэкенд (models.MotionWindow).
    tables = [worker.Camera.__table__, worker.VideoSegment.__table__,
              worker.MotionWindow.__table__]
    for t in tables:
        t.drop(engine, checkfirst=True)
        t.create(engine, checkfirst=True)

    # `cleanup_old()` чистит и события распознавания, поэтому таблица нужна
    # даже там, где тест про сегменты. Через ORM её не создать на SQLite:
    # `FaceEvent.embedding` — `Vector(512)` из pgvector. Ротации нужен один
    # столбец `ts`, поэтому таблица заводится минимальным DDL на обоих
    # движках — тест про сегменты, а не про схему событий.
    with engine.begin() as conn:
        conn.execute(_sql("DROP TABLE IF EXISTS face_events"))
        conn.execute(_sql(
            "CREATE TABLE face_events (id INTEGER PRIMARY KEY, ts TIMESTAMP)"
            if not is_pg else
            "CREATE TABLE face_events (id SERIAL PRIMARY KEY, ts TIMESTAMP)"))

    Session = sessionmaker(bind=engine)
    segdir = tmp_path / "segments"
    segdir.mkdir()

    monkeypatch.setattr(worker, "Session", Session)
    monkeypatch.setattr(worker, "MEDIA_PATH", str(tmp_path))
    # prune_media обходит подкаталоги медиа; каталоги под снимки нужны,
    # иначе уборка падает на несуществующем пути.
    for sub in ("snapshots", "avatars"):
        (tmp_path / sub).mkdir()

    class _Helper:
        def __init__(self):
            self.Session = Session
            self.dir = segdir

        def camera(self, cam_id, retention_days=None):
            with Session() as s:
                s.add(worker.Camera(id=cam_id, name=f"cam{cam_id}", enabled=True,
                                    mode="record_only", status="online",
                                    retention_days=retention_days))
                s.commit()

        def segment(self, cam_id, days_ago, size=4096):
            path = segdir / f"cam{cam_id}_{int(days_ago * 86400)}.mp4"
            path.write_bytes(b"\0" * size)
            started = NOW - timedelta(days=days_ago)
            with Session() as s:
                s.add(worker.VideoSegment(
                    camera_id=cam_id, started_at=started,
                    ended_at=started + timedelta(minutes=5),
                    file_path=str(path), event_type="continuous",
                    duration_sec=300, size_bytes=size))
                s.commit()
            return path

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
        with engine.begin() as conn:
            conn.execute(_sql("DROP TABLE IF EXISTS face_events"))
        for t in tables:
            t.drop(engine, checkfirst=True)
    engine.dispose()


def _freeze_now(monkeypatch):
    """`cleanup_old()` берёт время через datetime.utcnow() — подменяем, чтобы
    возраст сегментов не зависел от даты прогона (урок цикла 20)."""
    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return NOW
    monkeypatch.setattr(worker, "datetime", _DT)


# --- retention (SPEC §5) --------------------------------------------------

def test_per_camera_retention_applied_to_real_rows(archive, monkeypatch):
    """Две камеры, разная глубина — чистятся по-разному.

    Именно то, что до этого цикла было невозможно: обе камеры сравнивались
    с одним глобальным cutoff.
    """
    _freeze_now(monkeypatch)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 14)
    archive.camera(1, retention_days=3)
    archive.camera(2, retention_days=30)
    short = archive.segment(1, days_ago=7)
    long_kept = archive.segment(2, days_ago=7)

    worker.cleanup_old()

    assert archive.rows() == {os.path.basename(long_kept)}
    assert not short.exists(), "файл просроченного сегмента должен быть удалён"
    assert long_kept.exists()


def test_camera_without_own_retention_uses_global(archive, monkeypatch):
    """Позитивный контроль: NULL — «как глобально», а не «никогда»."""
    _freeze_now(monkeypatch)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 5)
    archive.camera(1)
    old = archive.segment(1, days_ago=9)
    fresh = archive.segment(1, days_ago=1)

    worker.cleanup_old()

    assert archive.rows() == {os.path.basename(fresh)}
    assert not old.exists()


def test_candidate_query_does_not_miss_short_retention_camera(archive, monkeypatch):
    """Сторож отбора кандидатов.

    Камера со сроком 1 день при глобальном 30: если бы кандидаты
    выбирались по глобальному сроку (или по максимальному), её
    двухдневный сегмент не попал бы в выборку вообще и не удалился бы,
    сколько бы раз ротация ни шла.
    """
    _freeze_now(monkeypatch)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 30)
    archive.camera(1, retention_days=1)
    doomed = archive.segment(1, days_ago=2)

    worker.cleanup_old()

    assert archive.rows() == set()
    assert not doomed.exists()


# --- циклическая перезапись (SPEC §5, §21) --------------------------------

class _Usage:
    """Подмена `shutil.disk_usage`: заполнение диска задаётся тестом.

    Настоящий диск песочницы для этого не годится — заполнить его до 95%
    ради проверки порога значило бы записать десятки гигабайт.
    """

    def __init__(self, total, free):
        self.total, self.free, self.used = total, free, total - free


def test_no_eviction_when_disk_has_room(archive, monkeypatch):
    """Позитивный контроль: на здоровом диске архив не трогается вовсе."""
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(1000, 900))
    monkeypatch.setitem(worker.CONFIG, "disk_min_free_pct", 5)
    archive.camera(1)
    kept = archive.segment(1, days_ago=99)

    assert worker.enforce_disk_quota() == 0
    assert archive.rows() == {os.path.basename(kept)}
    assert kept.exists()


def test_eviction_removes_oldest_until_target_met(archive, monkeypatch):
    """Диск заполнен — сносятся старейшие, пока не освободится порог.

    Свободно 10 из 1000 при пороге 5% → нужно освободить 40 байт.
    Сегменты по 30 байт: хватает двух старейших.
    """
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(1000, 10))
    monkeypatch.setitem(worker.CONFIG, "disk_min_free_pct", 5)
    archive.camera(1)
    oldest = archive.segment(1, days_ago=10, size=30)
    older = archive.segment(1, days_ago=9, size=30)
    newest = archive.segment(1, days_ago=1, size=30)

    dropped = worker.enforce_disk_quota()

    assert dropped == 2
    assert archive.rows() == {os.path.basename(newest)}
    assert not oldest.exists() and not older.exists()
    assert newest.exists()


def test_eviction_ignores_retention(archive, monkeypatch):
    """Перезапись сносит сегменты внутри срока хранения — SPEC §21."""
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(1000, 0))
    monkeypatch.setitem(worker.CONFIG, "disk_min_free_pct", 5)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 3650)
    archive.camera(1, retention_days=3650)
    fresh = archive.segment(1, days_ago=0, size=100)

    assert worker.enforce_disk_quota() == 1
    assert not fresh.exists()


def test_unreadable_disk_does_not_delete_anything(archive, monkeypatch):
    """`disk_usage` упал — не повод сносить архив."""
    def _boom(_p):
        raise OSError("нет такого пути")
    monkeypatch.setattr(worker.shutil, "disk_usage", _boom)
    archive.camera(1)
    kept = archive.segment(1, days_ago=99)

    assert worker.enforce_disk_quota() == 0
    assert kept.exists()


# --- миниатюры архива (SPEC §7) -------------------------------------------

def _thumb_for(archive, path, media_root):
    """Положить миниатюру для сегмента с файлом `path` и вернуть её путь."""
    import thumbs

    with archive.Session() as s:
        seg = s.query(worker.VideoSegment).filter_by(file_path=str(path)).one()
        seg_id = seg.id
    thumb = thumbs.thumb_path(str(media_root), seg_id)
    os.makedirs(os.path.dirname(thumb), exist_ok=True)
    with open(thumb, "wb") as fh:
        fh.write(b"\xff\xd8jpeg")
    return thumb


def test_retention_removes_thumbnail_with_segment(archive, monkeypatch, tmp_path):
    """Миниатюра (§7) уходит с диска вместе со своим сегментом.

    Она лежит в `thumbs/`, а не в `segments/`, поэтому под `prune_media`
    (чистит каталог сегментов по возрасту файла) не попадает вовсе. Без
    явного удаления миниатюра пережила бы сегмент **навсегда**: id сегмента
    больше никогда не повторится, значит и перезаписать её некому. На
    объекте это выглядело бы как медленно растущий диск без единой ошибки
    в логах — то есть нашлось бы не раньше, чем закончилось бы место.
    """
    _freeze_now(monkeypatch)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 3)
    archive.camera(1)
    expired = archive.segment(1, days_ago=7)
    thumb = _thumb_for(archive, expired, tmp_path)

    worker.cleanup_old()

    assert not expired.exists(), "сам сегмент должен быть удалён"
    assert not os.path.exists(thumb), "миниатюра пережила свой сегмент"


def test_eviction_removes_thumbnail_too(archive, monkeypatch, tmp_path):
    """Циклическая перезапись (§5) тоже уносит миниатюру.

    Отдельная проверка, а не дубль предыдущей: у ротации по сроку и у
    перезаписи по месту разные точки входа (`cleanup_old` и
    `enforce_disk_quota`), и удаление файлов у них общее только пока
    `_drop_segments` один на обе.
    """
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(1000, 0))
    monkeypatch.setitem(worker.CONFIG, "disk_min_free_pct", 5)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 3650)
    archive.camera(1, retention_days=3650)
    fresh = archive.segment(1, days_ago=0, size=100)
    thumb = _thumb_for(archive, fresh, tmp_path)

    assert worker.enforce_disk_quota() == 1

    assert not fresh.exists()
    assert not os.path.exists(thumb), "миниатюра пережила вытесненный сегмент"


def test_retention_keeps_thumbnail_of_live_segment(archive, monkeypatch, tmp_path):
    """Позитивный контроль: миниатюра непросроченного сегмента остаётся.

    Без него предыдущие две проверки прошли бы и на коде, который сносит
    каталог миниатюр целиком.
    """
    _freeze_now(monkeypatch)
    monkeypatch.setitem(worker.CONFIG, "retention_days", 30)
    archive.camera(1)
    kept = archive.segment(1, days_ago=1)
    thumb = _thumb_for(archive, kept, tmp_path)

    worker.cleanup_old()

    assert kept.exists()
    assert os.path.exists(thumb), "миниатюра живого сегмента удалена"


# --- алерты (SPEC §14) ----------------------------------------------------

def test_disk_alert_levels_follow_configured_thresholds(archive, monkeypatch):
    monkeypatch.setitem(worker.CONFIG, "disk_warn_pct", 80)
    monkeypatch.setitem(worker.CONFIG, "disk_crit_pct", 90)
    # Кулдаун через Redis: в тестах Redis может быть недоступен, и функция
    # обязана продолжать работать — проверяем заодно и это.
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)

    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(100, 30))
    assert worker.check_disk_alerts() is None          # занято 70%
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(100, 15))
    assert worker.check_disk_alerts() == "warning"     # занято 85%
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(100, 5))
    assert worker.check_disk_alerts() == "critical"    # занято 95%


def test_disk_alert_survives_redis_failure(archive, monkeypatch):
    """Отказ Redis не должен глушить алерт о заполнении диска."""
    def _boom(*a, **k):
        raise RuntimeError("redis недоступен")
    monkeypatch.setattr(worker.r, "set", _boom)
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _p: _Usage(100, 2))
    assert worker.check_disk_alerts() == "critical"


# --- параллельные проходы уборки (SPEC §5, §2) ----------------------------
#
# Три прохода уборки выбирают кандидатов независимо и по пересекающимся
# признакам: старейший сегмент переполненного диска обычно и просрочен тоже.
# Пока все три шли в одной нити менеджера, пересечься они не могли. С
# выносом retention в свою нить (§2: долгий этап не задерживает соседние)
# совпадение выборок стало штатным, и путь удаления обязан его пережить.

def test_a_second_pass_over_the_same_segment_does_not_break(archive, monkeypatch):
    """Два прохода выбрали один сегмент — второй не падает и не врёт в счёте.

    Проверка откатом: верните `s.delete(seg)` вместо `DELETE ... WHERE id IN`
    в `_drop_segments` — тест падает на `dropped_b == 0`, потому что счёт
    берётся из длины списка.

    **Заодно этот откат опровергает разбор цикла 55**, который и отложил
    правку: там ожидался `StaleDataError` и обрыв прохода. На откате его нет
    ни на SQLite, ни на настоящем Postgres 16 — SQLAlchemy печатает
    `SAWarning` и продолжает (`orm/persistence.py`: ошибку она бросает
    только при настроенном `version_id_col`). То есть опасность, ради
    которой цикл 55 не стал выносить уборку в свою нить, была не той, что
    предполагалась: ломался счёт, а не проход.
    """
    archive.camera(1)
    archive.segment(1, days_ago=9)

    with archive.Session() as first, archive.Session() as second:
        # Обе выборки сделаны до того, как первая что-либо удалила, — так и
        # получается на объекте: кандидаты набраны, дальше идёт долгий unlink.
        victims_a = first.query(worker.VideoSegment).all()
        victims_b = second.query(worker.VideoSegment).all()
        assert len(victims_a) == len(victims_b) == 1

        assert worker._drop_segments(first, victims_a) == 1
        first.commit()

        # Ключевое: второй проход доходит до конца.
        dropped_b = worker._drop_segments(second, victims_b)
        second.commit()

    assert dropped_b == 0, (
        "опоздавший проход записал в актив чужое удаление — на этих числах "
        "стоят логи ротации и отчёт §9 о снятом объёме"
    )
    assert archive.rows() == set()


def test_the_rest_of_the_batch_survives_a_stolen_segment(archive, monkeypatch):
    """Пачка из трёх, один уже снесён соседом — двое остальных удалены.

    Отдельно от предыдущей: там проверялось, что проход не падает, здесь —
    что он **доделывает работу**. Разница практическая: на объекте пачка
    циклической перезаписи — до 5000 строк, и обрыв на первой пересёкшейся
    означал бы, что диск не освобождается вовсе.
    """
    archive.camera(1)
    for days in (9, 8, 7):
        archive.segment(1, days_ago=days)

    with archive.Session() as first, archive.Session() as second:
        victims = second.query(worker.VideoSegment).order_by(
            worker.VideoSegment.started_at).all()
        assert len(victims) == 3

        stolen = first.query(worker.VideoSegment).order_by(
            worker.VideoSegment.started_at).first()
        assert worker._drop_segments(first, [stolen]) == 1
        first.commit()

        dropped = worker._drop_segments(second, victims)
        second.commit()

    assert dropped == 2, "счёт должен считать строки, а не намерения"
    assert archive.rows() == set(), "остаток пачки не удалён"


def test_dropped_segments_are_detached_not_expired(archive):
    """После удаления объекты отцеплены от сессии, а не просрочены.

    Без `expunge` commit пометил бы их expired, и первое же обращение к полю
    (в логе, в счётчике объёма) ушло бы за строкой, которой уже нет, —
    `ObjectDeletedError` уже после того, как работа сделана.
    """
    archive.camera(1)
    archive.segment(1, days_ago=9)

    with archive.Session() as s:
        victims = s.query(worker.VideoSegment).all()
        worker._drop_segments(s, victims)
        s.commit()
        # Обращение к полю после коммита не должно ходить в БД.
        assert victims[0].file_path.endswith(".mp4")
