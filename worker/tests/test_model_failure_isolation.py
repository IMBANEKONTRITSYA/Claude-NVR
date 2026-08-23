"""Отказ загрузки модели не должен ронять воркер (SPEC §2).

SPEC §2 требует буквально: «Отказ аналитики НЕ влияет на запись». До этого
фикса `manager()` вызывал `load_face_app()` голым вызовом первой же
строкой, и любая его ошибка убивала процесс — вместе со слоем записи,
индексацией сегментов, статусами камер и ONVIF-API.

Случай не гипотетический, а штатный: InsightFace скачивает модель из
интернета при первом запуске, а production-сервер видеонаблюдения обычно
изолирован. У пользователя это дало бесконечный CrashLoop контейнера,
запись не велась вообще, а автообнаружение камер отвечало «Сервис
распознавания недоступен» — на запрос, распознавания не касающийся.

Проверяется поведение (что происходит при отказе), а не форма вызова.

Требует полный requirements.txt воркера (урок цикла 16).
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

from embed_api import MODEL_UNAVAILABLE, build_app  # noqa: E402


@pytest.fixture(autouse=True)
def clean_model_state(monkeypatch):
    monkeypatch.setattr(worker, "FACE_APP", None)
    monkeypatch.setattr(worker, "MODEL_ERROR", None)


def _fail_load(monkeypatch, exc=RuntimeError("нет доступа к интернету")):
    def _boom(*a, **k):
        raise exc
    monkeypatch.setattr(worker, "load_face_app", _boom)


# --- загрузка модели ------------------------------------------------------

def test_model_failure_does_not_raise(monkeypatch):
    """Ключевое: отказ загрузки возвращает False, а не летит наверх.

    Именно вылет исключения из `manager()` и убивал процесс.
    """
    _fail_load(monkeypatch)
    assert worker._try_load_model() is False


def test_model_failure_records_reason_for_the_operator(monkeypatch):
    """Причина сохраняется: молчаливое «аналитика не работает» отличить от
    «в кадре никого нет» невозможно."""
    _fail_load(monkeypatch, ConnectionError("Failed to resolve 'github.com'"))
    worker._try_load_model()
    assert worker.MODEL_ERROR
    assert "github.com" in worker.MODEL_ERROR


def test_successful_load_clears_previous_error(monkeypatch):
    """Позитивный контроль: удачная дозагрузка снимает сообщение об отказе.

    Без этого интерфейс продолжал бы показывать старую аварию на
    работающей аналитике.
    """
    _fail_load(monkeypatch)
    worker._try_load_model()
    assert worker.MODEL_ERROR is not None

    monkeypatch.setattr(worker, "load_face_app", lambda *a, **k: object())
    assert worker._try_load_model() is True
    assert worker.MODEL_ERROR is None


# --- слой записи продолжает работать (SPEC §2) ----------------------------

def test_record_layer_publishes_status_without_model(monkeypatch):
    """Слой записи не зависит от модели.

    Смысловая проверка §2: при неработающей аналитике статусы потоков
    записи обязаны продолжать собираться и публиковаться.
    """
    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            return {"cam1": {"name": "cam1", "available": True, "ready": True,
                    "online": True, "inboundBytes": 4096}}

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segments", lambda ids: {})
    monkeypatch.setattr(worker, "update_status", lambda *a, **k: None)
    published = {}
    monkeypatch.setattr(worker.r, "set", lambda k, v, **kw: published.update({k: v}))

    payload = worker.publish_record_layer_status([(1, "Проходная")])

    assert payload["summary"]["streams_online"] == 1
    assert "record:layer" in published


def test_analytics_state_is_published_for_the_interface(monkeypatch):
    """Состояние аналитики едет в интерфейс вместе со статусом записи."""
    _fail_load(monkeypatch, RuntimeError("нет сети"))
    worker._try_load_model()

    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            return {}

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segments", lambda ids: {})
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)

    payload = worker.publish_record_layer_status([])

    assert payload["analytics"]["model_ready"] is False
    assert "нет сети" in payload["analytics"]["error"]


# --- HTTP-API воркера живёт без модели ------------------------------------

def test_worker_api_serves_onvif_without_a_model():
    """Автообнаружение камер не зависит от модели распознавания.

    Корень жалобы пользователя: ONVIF-роуты висели в приложении, которое
    нельзя было создать без модели, поэтому «Найти камеры в сети» отвечало
    «Сервис распознавания недоступен».
    """
    # `iter_route_contexts`, а не обход `app.routes`: с FastAPI 0.141
    # подключённый роутер лежит там единственным объектом-обёрткой, и
    # наивный обход не видит его роутов вовсе (то же, что в
    # backend/tests/test_audit_route_coverage.py).
    from fastapi.routing import iter_route_contexts

    app = build_app(lambda: None)
    paths = {ctx.route.path for ctx in iter_route_contexts(app.routes)}
    assert "/onvif/discover" in paths
    assert "/onvif/describe" in paths


def test_health_reports_model_state_separately():
    """`/health` различает «воркер жив» и «модель готова».

    Снаружи это раньше было неразличимо, и отказ модели читался как отказ
    всего воркера.
    """
    from fastapi.testclient import TestClient

    with TestClient(build_app(lambda: None)) as c:
        body = c.get("/health").json()
    assert body["ok"] is True and body["model_ready"] is False

    with TestClient(build_app(lambda: object())) as c:
        assert c.get("/health").json()["model_ready"] is True


def test_embed_explains_why_search_is_unavailable():
    """Поиск по фото без модели отвечает объяснением, а не 500."""
    from fastapi.testclient import TestClient

    with TestClient(build_app(lambda: None)) as c:
        r = c.post("/embed", files={"file": ("a.jpg", b"not-an-image", "image/jpeg")})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["error"] == MODEL_UNAVAILABLE


# --- недоступный Control API виден, а не молчит ---------------------------

def _broken_api(monkeypatch, exc=OSError("Connection refused")):
    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            raise exc

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segments", lambda ids: {})
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)
    monkeypatch.setattr(worker, "_record_api_error", None)
    monkeypatch.setattr(worker, "_record_prev_status", None)


def test_control_api_failure_reason_reaches_the_interface(monkeypatch):
    """Причина недоступности Control API обязана быть видимой.

    Пока она писалась на debug, отказ выглядел так: статусы камер молча
    замирали, интерфейс показывал всю стену офлайн, и в журнале не было ни
    строчки — понять, что сломался именно Control API, было нечем.
    """
    _broken_api(monkeypatch)
    payload = worker.publish_record_layer_status([(1, "Проходная")])

    assert payload["control_api_error"]
    assert "Connection refused" in payload["control_api_error"]


def test_control_api_failure_leaves_statuses_untouched(monkeypatch):
    """Недоступный Control API — «неизвестно», а не «камера пропала».

    Позитивный контроль к предыдущему: статус в БД не трогается, иначе
    рестарт MediaMTX гасил бы всю стену из 120 камер разом.
    """
    _broken_api(monkeypatch)
    written = []
    monkeypatch.setattr(worker, "update_status",
                        lambda *a, **k: written.append(a))

    payload = worker.publish_record_layer_status([(1, "Проходная")])

    assert written == []
    assert payload["summary"]["streams_unknown"] == 1


def test_recovered_control_api_clears_the_reason(monkeypatch):
    """Восстановление связи снимает сообщение: иначе интерфейс показывал бы
    старую аварию на здоровом медиасервере."""
    _broken_api(monkeypatch)
    worker.publish_record_layer_status([(1, "Проходная")])
    assert worker._record_api_error is not None

    class _Ok:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            return {"cam1": {"name": "cam1", "online": True, "inboundBytes": 1}}

    monkeypatch.setattr(worker, "MediaMTXClient", _Ok)
    monkeypatch.setattr(worker, "update_status", lambda *a, **k: None)
    payload = worker.publish_record_layer_status([(1, "Проходная")])

    assert payload["control_api_error"] is None
    assert worker._record_api_error is None
