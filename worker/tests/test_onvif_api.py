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
    assert r.json() == {"ok": True, "devices": []}


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
