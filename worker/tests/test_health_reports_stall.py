"""`/health` воркера обязан отвечать за живость менеджера, а не своей нити.

**Что здесь проверяется и почему это не мелочь.** Healthcheck контейнера
воркера (docker-compose.yml) дёргает именно этот эндпоинт. До цикла 38 он
отвечал безусловным `{"ok": true}`, а обслуживает его uvicorn в отдельной
нити (`start_embed_api`) — то есть он отвечал «жив» и тогда, когда
`manager()` намертво встал: слой записи не синхронизируется, сегменты не
индексируются, retention и циклическая перезапись не работают. Ровно тот
случай, ради которого healthcheck существует, он и не ловил.

Проверка идёт по production path: настоящее приложение из `build_app()`,
настоящий HTTP-запрос через TestClient, настоящий `liveness.Heartbeat` —
а не вызов функции-обработчика напрямую.

Требует полный requirements.txt воркера (cv2 тянет `embed_api`).
"""
import pytest

pytest.importorskip("cv2", reason="требует полный requirements.txt воркера")
pytest.importorskip("fastapi", reason="нужен FastAPI")

from fastapi.testclient import TestClient  # noqa: E402

import liveness  # noqa: E402
from embed_api import build_app  # noqa: E402


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, sec):
        self.now += sec


def _client(heartbeat=None):
    return TestClient(build_app(lambda: None, heartbeat=heartbeat), raise_server_exceptions=False)


def test_healthy_manager_reports_200():
    """Позитивный контроль: работающий воркер обязан оставаться здоровым.

    Без него «фикс», красящий healthcheck всегда, прошёл бы весь набор — и
    уронил бы воркер на ровном месте.
    """
    clock = FakeClock()
    hb = liveness.Heartbeat(budgets={"idle": 60.0}, clock=clock)
    hb.beat("idle")
    clock.advance(9.0)
    r = _client(hb).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["stage"] == "idle"
    assert body["stage_sec"] == pytest.approx(9.0)


def test_stalled_manager_fails_the_healthcheck():
    """Главная проверка: зависший менеджер обязан красить healthcheck."""
    clock = FakeClock()
    hb = liveness.Heartbeat(budgets={"index_segments": 30.0}, clock=clock)
    hb.beat("index_segments")
    clock.advance(31.0)

    r = _client(hb).get("/health")
    assert r.status_code == 503, (
        "healthcheck контейнера смотрит на код ответа: с 200 зависший "
        "воркер снова числился бы здоровым"
    )
    body = r.json()
    assert body["ok"] is False and body["stalled"] is True
    # Причина обязана быть в ответе: оператор смотрит в него, а не в стек.
    assert body["stage"] == "index_segments"
    assert "index_segments" in body["error"]


def test_recovery_makes_health_green_again():
    """Рассосавшееся зависание не должно оставлять воркер красным навсегда."""
    clock = FakeClock()
    hb = liveness.Heartbeat(budgets={"cleanup": 30.0}, clock=clock)
    client = _client(hb)

    hb.beat("cleanup")
    clock.advance(31.0)
    assert client.get("/health").status_code == 503

    hb.beat("idle")           # проход всё-таки завершился
    assert client.get("/health").status_code == 200


def test_slow_but_legitimate_stage_stays_green():
    """Часовая уборка архива идёт долго законно — красить её нельзя."""
    clock = FakeClock()
    hb = liveness.Heartbeat(budgets={"cleanup": 900.0}, clock=clock)
    hb.beat("cleanup")
    clock.advance(600.0)
    assert _client(hb).get("/health").status_code == 200


def test_without_heartbeat_health_stays_compatible():
    """Без отметок эндпоинт ведёт себя как раньше.

    `build_app` используется и в тестах ONVIF-API, где менеджера нет вовсе:
    судить о зависании не по чему, и выдумывать вердикт нельзя.
    """
    r = _client(None).get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "model_ready": False}


def test_model_failure_does_not_fail_the_healthcheck():
    """SPEC §2: отказ аналитики не влияет на запись.

    Модель не загружена (`get_face_app()` → None), но воркер жив и пишет.
    Красить healthcheck из-за модели значило бы уронить запись всех камер
    из-за неработающей аналитики — решение цикла 26, и оно обязано
    пережить эту правку.
    """
    clock = FakeClock()
    hb = liveness.Heartbeat(budgets={"idle": 60.0}, clock=clock)
    hb.beat("idle")
    r = _client(hb).get("/health")
    assert r.status_code == 200
    assert r.json()["model_ready"] is False
