"""Юнит-тесты HTTP-обёртки WS-Discovery (onvif_api.py) — отдельно от
embed_api.py, у которого те же тяжёлые зависимости (cv2/insightface),
которых нет в CI worker-job (см. .github/workflows/ci.yml). onvif_api.py
тянет только fastapi + onvif_client, поэтому TestClient здесь работает без
полного стека воркера. discover_devices подменяется monkeypatch'ем — сама
UDP/multicast-рассылка уже проверена в test_onvif_client.py."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

import onvif_api


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(onvif_api.router)
    return TestClient(app)


def test_onvif_discover_returns_found_devices(monkeypatch):
    fake_devices = [{
        "address": "urn:uuid:1111", "host": "192.168.1.64", "port": 80,
        "xaddrs": ["http://192.168.1.64/onvif/device_service"], "scopes": [],
    }]
    monkeypatch.setattr(onvif_api, "discover_devices", lambda timeout: fake_devices)

    r = _client().get("/onvif/discover")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["devices"] == fake_devices


def test_onvif_discover_returns_empty_list_when_nothing_found(monkeypatch):
    monkeypatch.setattr(onvif_api, "discover_devices", lambda timeout: [])

    r = _client().get("/onvif/discover")

    assert r.status_code == 200
    # warnings появился вместе с перебором подсети: multicast может не
    # пройти, а перебор — найти камеры, и такой частичный сбой не должен
    # выглядеть как ошибка. Здесь ошибок не было, поэтому список пуст.
    assert r.json() == {"ok": True, "devices": [], "warnings": []}


def test_onvif_discover_reports_onvif_error_without_500(monkeypatch):
    def _raise(timeout):
        raise onvif_api.OnvifError("не удалось отправить WS-Discovery Probe: network unreachable")
    monkeypatch.setattr(onvif_api, "discover_devices", _raise)

    r = _client().get("/onvif/discover")

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "network unreachable" in body["error"]
    assert body["devices"] == []


def test_onvif_discover_passes_timeout_query_param(monkeypatch):
    captured = {}
    monkeypatch.setattr(onvif_api, "discover_devices", lambda timeout: captured.setdefault("timeout", timeout) or [])

    r = _client().get("/onvif/discover?timeout=1.5")

    assert r.status_code == 200
    assert captured["timeout"] == 1.5


def test_onvif_discover_rejects_timeout_out_of_bounds():
    r = _client().get("/onvif/discover?timeout=100")
    assert r.status_code == 422


def test_onvif_profiles_returns_found_profiles(monkeypatch):
    fake_profiles = [{"token": "profile_1", "name": "MainStream"}]
    captured = {}

    def fake_get_profiles(host, port, username, password):
        captured.update(host=host, port=port, username=username, password=password)
        return fake_profiles

    monkeypatch.setattr(onvif_api, "get_profiles", fake_get_profiles)

    r = _client().post("/onvif/profiles", json={
        "host": "192.168.1.64", "port": 80, "username": "admin", "password": "s3cret",
    })

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["profiles"] == fake_profiles
    assert captured == {"host": "192.168.1.64", "port": 80, "username": "admin", "password": "s3cret"}


def test_onvif_profiles_defaults_port_and_optional_credentials(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        onvif_api, "get_profiles",
        lambda host, port, username, password: captured.setdefault("args", (host, port, username, password)) or [],
    )

    r = _client().post("/onvif/profiles", json={"host": "192.168.1.64"})

    assert r.status_code == 200
    assert captured["args"] == ("192.168.1.64", 80, None, None)


def test_onvif_profiles_reports_onvif_error_without_500(monkeypatch):
    def _raise(host, port, username, password):
        raise onvif_api.OnvifError("ONVIF-запрос к http://192.168.1.64:80/onvif/Media не удался: timed out")
    monkeypatch.setattr(onvif_api, "get_profiles", _raise)

    r = _client().post("/onvif/profiles", json={"host": "192.168.1.64"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "timed out" in body["error"]
    assert body["profiles"] == []


def test_onvif_profiles_requires_host():
    r = _client().post("/onvif/profiles", json={})
    assert r.status_code == 422


def test_onvif_stream_uri_returns_uri(monkeypatch):
    captured = {}

    def fake_get_stream_uri(host, port, profile_token, username, password):
        captured.update(host=host, port=port, profile_token=profile_token, username=username, password=password)
        return "rtsp://192.168.1.64:554/profile1"

    monkeypatch.setattr(onvif_api, "get_stream_uri", fake_get_stream_uri)

    r = _client().post("/onvif/stream-uri", json={
        "host": "192.168.1.64", "port": 80, "profile_token": "profile_1",
        "username": "admin", "password": "s3cret",
    })

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    # Камера отдаёт адрес без учётных данных (так велит спецификация ONVIF),
    # но ffmpeg/OpenCV читают логин и пароль только из самого URL — без
    # подстановки автозаполненный в форму адрес сразу давал 401 Unauthorized.
    assert body["uri"] == "rtsp://admin:s3cret@192.168.1.64:554/profile1"
    assert captured == {
        "host": "192.168.1.64", "port": 80, "profile_token": "profile_1",
        "username": "admin", "password": "s3cret",
    }


def test_onvif_stream_uri_reports_onvif_error_without_500(monkeypatch):
    def _raise(host, port, profile_token, username, password):
        raise onvif_api.OnvifError("в ответе GetStreamUri нет адреса потока (Uri)")
    monkeypatch.setattr(onvif_api, "get_stream_uri", _raise)

    r = _client().post("/onvif/stream-uri", json={"host": "192.168.1.64", "profile_token": "profile_1"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "нет адреса потока" in body["error"]
    assert body["uri"] is None


def test_onvif_stream_uri_requires_profile_token():
    r = _client().post("/onvif/stream-uri", json={"host": "192.168.1.64"})
    assert r.status_code == 422


def test_onvif_discover_with_subnet_merges_scan_results(monkeypatch):
    """Параметр subnet включает перебор адресов в дополнение к multicast.

    Нужен потому, что WS-Discovery рассылает multicast, а воркер живёт в
    docker-контейнере на NAT'ированной сети: на Docker Desktop под Windows
    такая рассылка до физической ЛВС не доходит, и поиск не находит ничего.
    """
    monkeypatch.setattr(onvif_api, "discover_devices", lambda timeout: [])
    monkeypatch.setattr(
        onvif_api, "scan_subnet",
        lambda cidr: [{"address": "", "xaddrs": [], "scopes": [],
                       "host": "192.168.105.19", "port": 80}],
    )

    r = _client().get("/onvif/discover", params={"subnet": "192.168.105.0/24"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [d["host"] for d in body["devices"]] == ["192.168.105.19"]


def test_onvif_discover_deduplicates_across_both_methods(monkeypatch):
    """Камера, найденная и multicast'ом, и перебором, не должна дублироваться."""
    device = {"address": "urn:uuid:1", "xaddrs": [], "scopes": [],
              "host": "192.168.105.19", "port": 80}
    monkeypatch.setattr(onvif_api, "discover_devices", lambda timeout: [device])
    monkeypatch.setattr(onvif_api, "scan_subnet", lambda cidr: [dict(device)])

    body = _client().get("/onvif/discover", params={"subnet": "192.168.105.0/24"}).json()

    assert len(body["devices"]) == 1


def test_subnet_scan_succeeds_even_when_multicast_fails(monkeypatch):
    """Ровно ситуация пользователя: multicast не проходит через сеть Docker.
    Его сбой не должен превращать успешный перебор в ошибку — иначе фикс
    не помог бы там, где он и нужен."""
    def _raise(timeout):
        raise onvif_api.OnvifError("не удалось отправить WS-Discovery Probe: network unreachable")
    monkeypatch.setattr(onvif_api, "discover_devices", _raise)
    monkeypatch.setattr(
        onvif_api, "scan_subnet",
        lambda cidr: [{"address": "", "xaddrs": [], "scopes": [],
                       "host": "192.168.105.19", "port": 80}],
    )

    body = _client().get("/onvif/discover", params={"subnet": "192.168.105.0/24"}).json()

    assert body["ok"] is True, "перебор нашёл камеру — это успех, а не ошибка"
    assert [d["host"] for d in body["devices"]] == ["192.168.105.19"]
    assert body["warnings"], "но о сбое multicast стоит сообщить"


def test_bad_subnet_reported_without_500(monkeypatch):
    monkeypatch.setattr(onvif_api, "discover_devices", lambda timeout: [])

    def _raise(cidr):
        raise onvif_api.OnvifError("некорректный диапазон 'мусор'")
    monkeypatch.setattr(onvif_api, "scan_subnet", _raise)

    r = _client().get("/onvif/discover", params={"subnet": "мусор"})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "некорректный диапазон" in body["error"]
