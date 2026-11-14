"""PTZ-управление камерой через API (SPEC §4) — production path на реальном
Postgres, воркер подменяется.

Подменяется именно HTTP-клиент к воркеру, а не роутер: проверяется то, за
что отвечает бэкенд, — матрица прав, валидация скоростей, подстановка
учётных данных камеры из БД и превращение ответа воркера в ответ API. Сам
протокол ONVIF проверяется в `worker/tests/test_onvif_ptz.py`.

Три свойства, ради которых тесты написаны:

1. **Наблюдатель не может повернуть камеру.** §18 оставляет ему только
   просмотр, а поворот меняет обзор для всех дежурных сразу.
2. **Пароль ONVIF в команду подставляет сервер**, из БД, расшифровывая, —
   оператор управляет камерой, не зная её пароля и не имея возможности его
   получить.
3. **Камера без ONVIF отвечает понятным отказом**, а не 500 из недр
   прокси-вызова.
"""
import pytest

pytestmark = pytest.mark.usefixtures("client")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeWorker:
    """Подменяет httpx.AsyncClient в роутере камер и записывает вызовы."""

    def __init__(self, payload):
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def factory(self, *a, **kw):
        worker = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, json=None):
                worker.calls.append((url, json or {}))
                return _FakeResponse(worker.payload)

        return _Client()


@pytest.fixture()
def worker(monkeypatch):
    """Воркер, отвечающий «камера поворотная, команда принята»."""
    from app.routers import cameras as cameras_router

    fake = _FakeWorker({"ok": True, "supported": True, "profile_token": "main_1",
                        "presets": [{"token": "1", "name": "Ворота"}]})
    monkeypatch.setattr(cameras_router.httpx, "AsyncClient", fake.factory)
    return fake


@pytest.fixture()
def ptz_camera(make_camera):
    return make_camera(
        "ptz-cam", onvif_enabled=True, onvif_host="192.168.1.64", onvif_port=8000,
        onvif_username="admin", onvif_password="s3cret",
    )


# ---------------------------------------------------------------------------
# Матрица прав (§18)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("role,allowed", [("admin", True), ("operator", True), ("viewer", False)])
def test_ptz_move_follows_permission_matrix(client, make_user_headers, ptz_camera, worker,
                                            role, allowed):
    """Наблюдателю §18 оставляет только просмотр: поворот камеры уводит
    обзор с точки для всех остальных, а не меняет его личную картинку."""
    headers = make_user_headers(f"ptz-{role}", role)
    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move",
                    json={"pan": 0.5}, headers=headers)
    if allowed:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 403
        # И до камеры запрос не дошёл — отказ на входе, а не после поворота.
        assert worker.calls == []


@pytest.mark.parametrize("path,payload", [
    ("ptz/stop", {}),
    ("ptz/preset/goto", {"preset_token": "1"}),
    ("ptz/preset/save", {"name": "Ворота"}),
])
def test_all_ptz_commands_are_closed_to_viewer(client, make_user_headers, ptz_camera, worker,
                                               path, payload):
    """Проверка всего класса, а не одного эндпоинта: закрыть move и забыть
    про stop/пресеты — ровно тот вид пробела, который ищется списком."""
    headers = make_user_headers("ptz-viewer-all", "viewer")
    r = client.post(f"/api/cameras/{ptz_camera['id']}/{path}", json=payload, headers=headers)
    assert r.status_code == 403
    assert worker.calls == []


def test_ptz_capabilities_closed_to_viewer(client, make_user_headers, ptz_camera, worker):
    headers = make_user_headers("ptz-viewer-caps", "viewer")
    assert client.get(f"/api/cameras/{ptz_camera['id']}/ptz", headers=headers).status_code == 403


def test_ptz_requires_authentication(client, ptz_camera):
    assert client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move", json={"pan": 0.5}).status_code == 401


# ---------------------------------------------------------------------------
# Учётные данные камеры
# ---------------------------------------------------------------------------

def test_ptz_command_carries_camera_credentials_from_db(client, admin_headers, ptz_camera, worker):
    """Оператор управляет камерой, не зная её пароля: пароль лежит в БД
    зашифрованным, расшифровывается на сервере и в ответ клиенту не уходит."""
    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move",
                    json={"pan": 0.5, "tilt": -0.25, "profile_token": "main_1"},
                    headers=admin_headers)
    assert r.status_code == 200, r.text

    url, sent = worker.calls[-1]
    assert url.endswith("/onvif/ptz/move")
    assert sent["host"] == "192.168.1.64" and sent["port"] == 8000
    assert sent["username"] == "admin" and sent["password"] == "s3cret"
    assert sent["pan"] == 0.5 and sent["tilt"] == -0.25
    assert "s3cret" not in r.text


def test_ptz_on_camera_without_onvif_is_a_clear_refusal(client, admin_headers, make_camera, worker):
    """PTZ без ONVIF невозможен физически — ответ должен это и говорить, а
    не падать где-то в прокси-вызове к воркеру."""
    cam = make_camera("fixed-cam")
    r = client.post(f"/api/cameras/{cam['id']}/ptz/move", json={"pan": 0.5}, headers=admin_headers)
    assert r.status_code == 400
    assert "ONVIF" in r.json()["detail"]
    assert worker.calls == []


def test_ptz_on_missing_camera_is_404(client, admin_headers, worker):
    assert client.get("/api/cameras/999999/ptz", headers=admin_headers).status_code == 404


# ---------------------------------------------------------------------------
# Валидация скоростей
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"pan": 5}, {"pan": -5}, {"tilt": 1.01}, {"zoom": -2},
])
def test_ptz_move_rejects_velocity_outside_range(client, admin_headers, ptz_camera, worker, payload):
    """Скорость вне [-1, 1] часть прошивок трактует по модулю и уводит
    камеру в обратную сторону; nan/inf как скорость не имеют смысла вовсе."""
    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move", json=payload, headers=admin_headers)
    assert r.status_code == 422
    assert worker.calls == []


@pytest.mark.parametrize("raw", ['{"pan": Infinity}', '{"pan": NaN}', '{"zoom": -Infinity}'])
def test_ptz_move_rejects_infinity_and_nan_velocities(client, admin_headers, ptz_camera, worker, raw):
    """Отдельно от проверки диапазона выше, потому что и путь другой:
    `Infinity`/`NaN` нельзя закодировать штатным JSON-клиентом, зато
    `json.loads` на стороне сервера их принимает и отдаёт как float. До
    границ они не сравниваются никак (любое сравнение с NaN ложно), поэтому
    один только `ge/le` их пропустил бы — держит `allow_inf_nan=False`.
    """
    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move", content=raw,
                    headers={**admin_headers, "Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert worker.calls == []


def test_ptz_move_accepts_range_bounds(client, admin_headers, ptz_camera, worker):
    """Обратная сторона проверки выше: сами границы — законные значения, и
    «валидация», отвергающая всё подряд, тестами не пройдёт."""
    for value in (-1.0, 0.0, 1.0):
        r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move",
                        json={"pan": value}, headers=admin_headers)
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# Ответы камеры
# ---------------------------------------------------------------------------

def test_capabilities_returns_presets(client, admin_headers, ptz_camera, worker):
    body = client.get(f"/api/cameras/{ptz_camera['id']}/ptz", headers=admin_headers).json()
    assert body["supported"] is True
    assert body["profile_token"] == "main_1"
    assert body["presets"] == [{"token": "1", "name": "Ворота"}]


def test_capabilities_of_unreachable_camera_does_not_hide_the_pad_behind_an_error(
        client, admin_headers, ptz_camera, monkeypatch):
    """Камера, моргнувшая сетью, не должна выглядеть как поломка интерфейса:
    ответ — «PTZ недоступен» с причиной, а не 502."""
    from app.routers import cameras as cameras_router
    fake = _FakeWorker({"ok": False, "error": "нет связи"})
    monkeypatch.setattr(cameras_router.httpx, "AsyncClient", fake.factory)

    r = client.get(f"/api/cameras/{ptz_camera['id']}/ptz", headers=admin_headers)

    assert r.status_code == 200
    assert r.json()["supported"] is False
    assert r.json()["error"] == "нет связи"


def test_move_rejected_by_camera_is_reported_as_bad_gateway(client, admin_headers, ptz_camera,
                                                            monkeypatch):
    from app.routers import cameras as cameras_router
    fake = _FakeWorker({"ok": False, "error": "NoConfig"})
    monkeypatch.setattr(cameras_router.httpx, "AsyncClient", fake.factory)

    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move",
                    json={"pan": 0.5}, headers=admin_headers)

    assert r.status_code == 502
    assert "NoConfig" in r.json()["detail"]


def test_save_preset_returns_refreshed_list(client, admin_headers, ptz_camera, monkeypatch):
    from app.routers import cameras as cameras_router
    fake = _FakeWorker({"ok": True, "preset_token": "3",
                        "presets": [{"token": "3", "name": "Касса"}]})
    monkeypatch.setattr(cameras_router.httpx, "AsyncClient", fake.factory)

    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/preset/save",
                    json={"name": "Касса"}, headers=admin_headers)

    assert r.status_code == 200
    assert r.json()["preset_token"] == "3"
    assert r.json()["presets"] == [{"token": "3", "name": "Касса"}]


def test_worker_unavailable_is_503_not_500(client, admin_headers, ptz_camera, monkeypatch):
    """Пульт должен сказать «сервис недоступен», а не показать внутреннюю
    ошибку: воркер перезапускается штатно (обновление, смена профиля)."""
    import httpx as real_httpx
    from app.routers import cameras as cameras_router

    class _Broken:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            raise real_httpx.ConnectError("connection refused")

    monkeypatch.setattr(cameras_router.httpx, "AsyncClient", lambda *a, **kw: _Broken())

    r = client.post(f"/api/cameras/{ptz_camera['id']}/ptz/move",
                    json={"pan": 0.5}, headers=admin_headers)

    assert r.status_code == 503
