"""Сессия БД не удерживается во время сетевого запроса снимка и инференса.

Находка цикла 20 (P1, 24/7-стабильность). Перенесённый пункт «карты
архитектурного долга» из отчёта цикла 19: в `process_faces()` весь разбор
кадра шёл внутри одного `with Session()`, включая HTTP-запрос снимка к
камере (таймаут 4 с) и прогон детектора по полноразмерному кадру.

Механизм удержания — `expire_on_commit=True`, умолчание `sessionmaker` в
`worker.py` (в отличие от бэкенда, где стоит `False`): после `s.commit()`
первое же обращение к `person.name` реактивировало объект, открывая новую
транзакцию, и она жила до следующего коммита — то есть сквозь всю сеть и
весь инференс. На 16 камерах это до 16 соединений в состоянии
`idle in transaction` одновременно; они держат горизонт видимости и не
дают autovacuum чистить `face_events`.

Тест проверяет это ровно так, как проблема проявляется в проде: спрашивает
у **настоящего Postgres** через `pg_stat_activity`, есть ли транзакции в
состоянии `idle in transaction`, — и спрашивает в тот момент, когда воркер
качает снимок. Никакого мока БД: наблюдается реальное состояние реальных
соединений реального пула.

Это production path, а не edge case: сценарий теста — обычный кадр с
лицом, прошедшим cooldown, то есть та самая ветка, по которой воркер идёт
при каждом событии. Подменены только две вещи, которых в песочнице
существовать не может, — сетевой запрос к камере и ML-модель.

Требует полный requirements.txt воркера для импорта worker.py, поэтому в
CI-джобе воркера пропускается (см. .github/workflows/ci.yml и
test_process_faces_snapshot_reuse.py — тот же приём importorskip).
"""
import pytest

# Все тяжёлые импорты — строго после importorskip (урок цикла 16).
worker = pytest.importorskip(
    "worker",
    reason="нужен полный requirements.txt воркера (cv2/numpy/sklearn/pgvector)",
)

import numpy as np  # noqa: E402
import sqlalchemy  # noqa: E402


class _FakeFace:
    """Лицо в том виде, в каком его отдаёт insightface.

    Вектор — единица в своей координате: такие векторы взаимно
    ортогональны, поэтому разные ident гарантированно дают разные персоны
    (вектор из одинаковых значений после нормировки даёт одно и то же
    направление при любом seed, и все лица схлопнулись бы в одну персону —
    урок цикла 19).
    """

    def __init__(self, bbox, ident: int):
        self.bbox = np.asarray(bbox, dtype=np.float32)
        vec = np.zeros(512, dtype=np.float32)
        vec[ident % 512] = 1.0
        self.normed_embedding = vec


class _StubDetector:
    def __init__(self, faces):
        self.faces = faces

    def get(self, frame):
        return self.faces


@pytest.fixture()
def live_services():
    """Пропуск, если Postgres/Redis недоступны (локальный прогон без
    docker-compose), — иначе здоровое дерево давало бы ложное «failed»
    (урок цикла 18)."""
    try:
        with worker.Session() as s:
            s.execute(worker.text("SELECT 1"))
        worker.r.ping()
    except Exception as e:
        pytest.skip(f"нет живых Postgres/Redis: {e}")


@pytest.fixture()
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "MEDIA_PATH", str(tmp_path))
    (tmp_path / "snapshots").mkdir()
    return tmp_path


@pytest.fixture()
def camera(live_services):
    with worker.Session() as s:
        cam = worker.Camera(
            name="test-cam-no-tx-during-network",
            rtsp_url_enc="x",
            location="",
            motion_sensitivity=0,
            enabled=False,
            status="offline",
        )
        s.add(cam)
        s.commit()
        cam_id = cam.id
    try:
        yield cam_id
    finally:
        with worker.Session() as s:
            s.execute(worker.delete(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id))
            s.execute(worker.delete(worker.Camera).where(worker.Camera.id == cam_id))
            s.commit()


def _idle_in_transaction_pids(probe_engine) -> list:
    """Бэкенды Postgres, висящие в `idle in transaction`, кроме собственного.

    Отдельный engine, а не сессия воркера: спрашивать пул о самом себе
    изнутри его же соединения бессмысленно — нужен взгляд со стороны, как
    у мониторинга в проде.
    """
    with probe_engine.connect() as c:
        rows = c.execute(sqlalchemy.text("""
            SELECT pid FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND state = 'idle in transaction'
        """)).fetchall()
    return [row[0] for row in rows]


def test_no_open_transaction_while_snapshot_is_fetched(camera, media_dir, monkeypatch):
    """Пока едет снимок с камеры, ни одно соединение воркера не в транзакции.

    До фикса `person.name` после `s.commit()` реактивировал объект и
    открывал транзакцию, которая жила до вставки события, — то есть
    ровно в момент, когда этот тест смотрит в pg_stat_activity, там был
    бы виден один `idle in transaction`.
    """
    cam_id = camera
    probe_engine = sqlalchemy.create_engine(worker.DATABASE_URL, poolclass=sqlalchemy.pool.NullPool)
    observed = {}

    hires_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    def _fake_fetch(url, timeout=4.0):
        # Момент истины: воркер сейчас «в сети». Всё, что открыто в БД
        # прямо сейчас, в проде висело бы до 4 секунд на камеру.
        observed["idle_in_tx"] = _idle_in_transaction_pids(probe_engine)
        return hires_frame

    monkeypatch.setattr(worker, "fetch_snapshot_frame", _fake_fetch)
    monkeypatch.setattr(worker, "FACE_APP", _StubDetector([_FakeFace((300, 300, 450, 450), 1)]))

    analytics_faces = [_FakeFace((100, 100, 150, 150), 1)]
    frame = np.zeros((360, 640, 3), dtype=np.uint8)

    try:
        worker.process_faces(
            cam_id, frame, analytics_faces, 640, 360,
            now=30_000.0, last_event_at={}, snapshot_url="http://camera/snapshot",
        )
    finally:
        probe_engine.dispose()

    assert "idle_in_tx" in observed, "снимок не запрашивался — тест ничего не проверил"
    assert observed["idle_in_tx"] == [], (
        "во время сетевого запроса снимка открыта транзакция БД "
        f"(pid {observed['idle_in_tx']}); на 16 камерах это столько же "
        "соединений в состоянии idle in transaction"
    )

    # Контроль того, что работа вообще была сделана, — иначе проверка выше
    # вырождается в «ничего не делали, поэтому и транзакций нет»
    # (урок цикла 19).
    with worker.Session() as s:
        events = s.execute(
            worker.select(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id)
        ).scalars().all()
    assert len(events) == 1, "событие должно быть создано"
    assert events[0].snapshot_path, "снимок лица должен быть сохранён"


def test_no_open_transaction_while_detector_runs_on_snapshot(camera, media_dir, monkeypatch):
    """То же для прогона модели по полноразмерному кадру.

    Инференс на 1920×1080 на слабом CPU (целевая платформа N100, ТЗ 18)
    занимает сотни миллисекунд — не 4 секунды таймаута, но на 16 камерах
    в сумме тоже держало бы транзакции постоянно открытыми.
    """
    cam_id = camera
    probe_engine = sqlalchemy.create_engine(worker.DATABASE_URL, poolclass=sqlalchemy.pool.NullPool)
    observed = {}

    class _ObservingDetector:
        def get(self, frame):
            observed["idle_in_tx"] = _idle_in_transaction_pids(probe_engine)
            return [_FakeFace((300, 300, 450, 450), 1)]

    monkeypatch.setattr(
        worker, "fetch_snapshot_frame",
        lambda url, timeout=4.0: np.zeros((1080, 1920, 3), dtype=np.uint8),
    )
    monkeypatch.setattr(worker, "FACE_APP", _ObservingDetector())

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    try:
        worker.process_faces(
            cam_id, frame, [_FakeFace((100, 100, 150, 150), 1)], 640, 360,
            now=40_000.0, last_event_at={}, snapshot_url="http://camera/snapshot",
        )
    finally:
        probe_engine.dispose()

    assert "idle_in_tx" in observed, "детектор не вызывался — тест ничего не проверил"
    assert observed["idle_in_tx"] == [], (
        "во время прогона детектора по снимку открыта транзакция БД "
        f"(pid {observed['idle_in_tx']})"
    )
