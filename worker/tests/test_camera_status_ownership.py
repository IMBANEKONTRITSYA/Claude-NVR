"""`Camera.status` ведёт слой записи, а не слой аналитики (SPEC §2, §4).

Регрессия цикла 24, проявившаяся у пользователя: `update_status()`
вызывалась только из `camera_worker()`, а цикл 24 перестал поднимать её
для камер в режиме `record_only`. Камера, которая исправно пишется,
навсегда оставалась `offline`; интерфейс гейтит HLS-плеер по
`status === "online"`, поэтому live-просмотр не работал ни на одной
`record_only`-камере — при штатной конфигурации это 118 камер из 120.

Проверяется поведение, а не форма: после прохода слоя записи статус в БД
обязан стать `online`, и обратно — `offline` при потере потока.

Требует полный requirements.txt воркера (урок цикла 16: тяжёлые импорты
строго после `importorskip`).
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

from sqlalchemy import create_engine, text as _sql  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """Своя БД с таблицей камер, подставленная воркеру.

    Схема Postgres — отдельная (урок цикла 25): модели воркера носят те же
    имена таблиц, что и модели бэкенда, и `create/drop` на общей БД снёс бы
    его схему.
    """
    url = os.environ.get("SEGMENT_INDEX_TEST_DATABASE_URL")
    url = url.replace("+asyncpg", "") if url else f"sqlite:///{tmp_path / 'st.db'}"
    is_pg = not url.startswith("sqlite")
    schema = "status_test"
    engine = create_engine(
        url, connect_args={"options": f"-csearch_path={schema}"} if is_pg else {})
    if is_pg:
        with create_engine(url).begin() as admin:
            admin.execute(_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
            admin.execute(_sql(f"CREATE SCHEMA {schema}"))
    worker.Camera.__table__.drop(engine, checkfirst=True)
    worker.Camera.__table__.create(engine, checkfirst=True)

    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(worker, "Session", Session)
    # Состояние между тестами не должно течь: обе структуры модульные.
    monkeypatch.setattr(worker, "_last_status", {})
    monkeypatch.setattr(worker, "_record_layer_owned", set())
    monkeypatch.setattr(worker, "_record_prev_status", None)
    # Redis в тестах не нужен — публикация статуса не проверяется здесь.
    monkeypatch.setattr(worker.r, "publish", lambda *a, **k: None)
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)

    class _Helper:
        def add(self, cam_id, name, mode="record_only", status="offline"):
            with Session() as s:
                s.add(worker.Camera(id=cam_id, name=name, enabled=True,
                                    mode=mode, status=status))
                s.commit()

        def status(self, cam_id):
            with Session() as s:
                return s.get(worker.Camera, cam_id).status

    yield _Helper()

    if is_pg:
        engine.dispose()
        with create_engine(url).begin() as admin:
            admin.execute(_sql(f"DROP SCHEMA IF EXISTS {schema} CASCADE"))
    else:
        worker.Camera.__table__.drop(engine, checkfirst=True)
    engine.dispose()


def _mediamtx(monkeypatch, paths):
    """Подменяет Control API MediaMTX заданным рантайм-ответом."""
    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            if paths is None:
                raise RuntimeError("Control API недоступен")
            return paths

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segment_ts", lambda ids: {})


def test_record_only_camera_goes_online_from_record_layer(db, monkeypatch):
    """Суть регрессии: камера без нити аналитики обязана становиться online.

    До фикса `record_only`-камера оставалась `offline` навсегда, и
    live-просмотр по ней был мёртв.
    """
    db.add(1, "Проходная", mode="record_only")
    _mediamtx(monkeypatch, {"cam1": {"name": "cam1", "online": True,
                                     "inboundBytes": 4096}})

    worker.publish_record_layer_status([(1, "Проходная")])

    assert db.status(1) == "online"


def test_lost_stream_returns_camera_to_offline(db, monkeypatch):
    """Обратное направление: потеря потока обязана гасить камеру."""
    db.add(1, "Проходная", status="online")
    _mediamtx(monkeypatch, {"cam1": {"name": "cam1", "online": False,
                                     "inboundBytes": 0}})

    worker.publish_record_layer_status([(1, "Проходная")])

    assert db.status(1) == "offline"


def test_unreachable_control_api_does_not_blank_all_cameras(db, monkeypatch):
    """`unknown` не затирает последний известный статус.

    Иначе рестарт MediaMTX гасил бы всю стену из 120 камер разом, а
    оператор читал бы это как массовую аварию.
    """
    db.add(1, "Проходная", status="online")
    _mediamtx(monkeypatch, None)

    worker.publish_record_layer_status([(1, "Проходная")])

    assert db.status(1) == "online"


def test_analytics_thread_does_not_override_record_layer(db, monkeypatch):
    """Слой записи владеет статусом, пока о камере есть данные.

    Без разделения владения нить аналитики зовёт `update_status("online")`
    на каждом кадре, слой записи раз в ~10 с ставит `offline`, и статус
    мигает с записью в БД и публикацией в Redis на каждом обороте.
    """
    db.add(1, "Проходная", mode="analytics", status="online")
    _mediamtx(monkeypatch, {"cam1": {"name": "cam1", "online": False,
                                     "inboundBytes": 0}})
    worker.publish_record_layer_status([(1, "Проходная")])
    assert db.status(1) == "offline"

    # Нить аналитики продолжает считать камеру живой — её вердикт
    # игнорируется, пока слой записи знает про камеру.
    worker.update_status(1, "online")
    assert db.status(1) == "offline"


def test_analytics_thread_is_authoritative_when_mediamtx_unknown(db, monkeypatch):
    """Позитивный контроль: без данных слоя записи аналитика снова главная.

    Ровно тот случай, ради которого владение снимается: MediaMTX не
    развёрнут или недоступен, но нить аналитики видит поток.
    """
    db.add(1, "Проходная", mode="analytics", status="offline")
    _mediamtx(monkeypatch, None)
    worker.publish_record_layer_status([(1, "Проходная")])

    worker.update_status(1, "online")
    assert db.status(1) == "online"


def test_camera_leaving_record_layer_releases_ownership(db, monkeypatch):
    """Выключенная камера не оставляет за собой владение статусом.

    Иначе её статус замёрз бы навсегда: слой записи о ней больше не
    сообщает, а вердикт аналитики продолжал бы отбрасываться.
    """
    db.add(1, "Проходная", mode="analytics")
    _mediamtx(monkeypatch, {"cam1": {"name": "cam1", "online": True,
                                     "inboundBytes": 1}})
    worker.publish_record_layer_status([(1, "Проходная")])
    assert 1 in worker._record_layer_owned

    # Камера ушла из списка слоя записи.
    worker.publish_record_layer_status([])
    assert 1 not in worker._record_layer_owned
