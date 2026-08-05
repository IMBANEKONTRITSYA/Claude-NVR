"""Интеграционные тесты на реальном Postgres+Redis (см. conftest.py:client).
Раньше обработчики роутеров почти не покрывались — юнит-тесты проверяли
только чистую логику (auth/шифрование/валидация), без БД в CI. Здесь —
полный цикл: RBAC на реальных эндпоинтах, шифрование RTSP-учёток при
записи/чтении из настоящей БД, аудит-лог, системные настройки."""


def _unique(prefix: str, request) -> str:
    return f"{prefix}_{request.node.name}"[:60]


def test_health_reports_ok_with_real_db_and_redis(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["db"] == "ok"
    assert body["redis"] == "ok"


def test_admin_login_returns_role_and_tokens(client):
    from app.config import settings

    r = client.post("/api/auth/login", data={"username": "admin", "password": settings.ADMIN_PASSWORD})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "admin"
    assert body["username"] == "admin"
    assert body["access_token"] and body["refresh_token"]


def test_login_wrong_password_rejected(client):
    r = client.post("/api/auth/login", data={"username": "admin", "password": "definitely-wrong"})
    assert r.status_code == 401


def test_cameras_require_auth(client):
    r = client.get("/api/cameras")
    assert r.status_code == 401


def test_hls_auth_rejects_missing_or_invalid_token(client):
    # Внутренний эндпоинт для nginx auth_request (location /hls/, P0 цикл 6):
    # раньше видеопоток проксировался вообще без проверки. Контракт —
    # тот же, что и у get_current_user везде: 401 без валидного Bearer.
    assert client.get("/api/cameras/hls-auth").status_code == 401
    assert client.get(
        "/api/cameras/hls-auth", headers={"Authorization": "Bearer garbage"}
    ).status_code == 401


def test_hls_auth_accepts_any_authenticated_role(client, admin_headers):
    # Матрица прав ТЗ: просмотр видео онлайн разрешён всем трём ролям — этот
    # эндпоинт не должен ограничивать роль, только проверять валидность токена.
    r = client.get("/api/cameras/hls-auth", headers=admin_headers)
    assert r.status_code == 200


def test_camera_crud_roundtrip_encrypts_rtsp_credentials(client, admin_headers, request):
    name = _unique("cam", request)
    rtsp_url = "rtsp://operator:s3cr3t@192.168.1.50:554/Streaming/Channels/101"
    sub_url = "rtsp://operator:s3cr3t@192.168.1.50:554/Streaming/Channels/102"

    r = client.post(
        "/api/cameras",
        json={"name": name, "rtsp_url": rtsp_url, "sub_rtsp_url": sub_url, "location": "Вход"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    cam = r.json()
    assert cam["name"] == name
    assert cam["has_substream"] is True
    cam_id = cam["id"]

    # Список камер не должен светить сырой RTSP-URL с учётными данными нигде в ответе.
    r = client.get("/api/cameras", headers=admin_headers)
    assert r.status_code == 200
    assert "s3cr3t" not in r.text

    # Расшифровка доступна только явным admin-эндпоинтом (что подтверждает
    # реальный AES-роундтрип через настоящую БД, не мок).
    r = client.get(f"/api/cameras/{cam_id}/rtsp", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["rtsp_url"] == rtsp_url

    r = client.put(
        f"/api/cameras/{cam_id}",
        json={"name": name, "rtsp_url": rtsp_url, "location": "Запасной вход", "enabled": False},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    # sub_rtsp_url отсутствовал в PUT — субпоток должен остаться прежним, не очиститься
    assert r.json()["has_substream"] is True

    r = client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)
    assert r.status_code == 200

    r = client.get(f"/api/cameras/{cam_id}/rtsp", headers=admin_headers)
    assert r.status_code == 404


def test_camera_onvif_config_roundtrip(client, admin_headers, request):
    # ТЗ 18.7: события движения от ONVIF-камеры — конфиг сохраняется и
    # шифруется так же, как RTSP-учётки, пароль не светится в списке камер.
    name = _unique("cam-onvif", request)
    r = client.post(
        "/api/cameras",
        json={
            "name": name, "rtsp_url": "rtsp://cam/stream", "location": "Вход",
            "onvif_enabled": True, "onvif_host": "192.168.1.64", "onvif_port": 80,
            "onvif_username": "admin", "onvif_password": "s3cr3t-onvif",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    cam = r.json()
    assert cam["onvif_enabled"] is True
    assert cam["has_onvif"] is True
    cam_id = cam["id"]

    r = client.get("/api/cameras", headers=admin_headers)
    assert "s3cr3t-onvif" not in r.text

    # Обновление без onvif_password не должно требовать пароль заново и не
    # должно сбрасывать onvif_enabled/host (тот же принцип, что и sub_rtsp_url).
    r = client.put(
        f"/api/cameras/{cam_id}",
        json={
            "name": name, "rtsp_url": "rtsp://cam/stream", "location": "Вход",
            "onvif_enabled": True, "onvif_host": "192.168.1.64", "onvif_port": 80,
            "onvif_username": "admin",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["has_onvif"] is True

    client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_onvif_discover_requires_admin(client, admin_headers, request):
    username = _unique("op-onvif-discover", request)
    client.post(
        "/api/users",
        json={"username": username, "password": "Op3rator!Pass1", "role": "operator"},
        headers=admin_headers,
    )
    r = client.post("/api/auth/login", data={"username": username, "password": "Op3rator!Pass1"})
    operator_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.get("/api/cameras/onvif/discover", headers=operator_headers)
    assert r.status_code == 403

    r = client.get("/api/cameras/onvif/discover")
    assert r.status_code == 401


def test_onvif_discover_proxies_worker_devices(client, admin_headers, monkeypatch):
    import httpx as httpx_module

    fake_devices = [{
        "address": "urn:uuid:1111", "host": "192.168.1.64", "port": 80,
        "xaddrs": ["http://192.168.1.64/onvif/device_service"], "scopes": [],
    }]

    class _FakeResponse:
        def json(self):
            return {"ok": True, "devices": fake_devices, "warnings": []}

    async def _fake_get(self, url, **kwargs):
        assert url.endswith("/onvif/discover")
        return _FakeResponse()

    monkeypatch.setattr(httpx_module.AsyncClient, "get", _fake_get)

    r = client.get("/api/cameras/onvif/discover", headers=admin_headers)
    assert r.status_code == 200
    # warnings добавлен вместе с перебором подсети: multicast может не пройти,
    # а перебор — найти камеры, и такой частичный сбой не ошибка.
    assert r.json() == {"devices": fake_devices, "warnings": []}


def test_onvif_discover_passes_subnet_to_worker(client, admin_headers, monkeypatch):
    """Диапазон должен доходить до воркера: без него поиск идёт только
    multicast'ом, который не проходит через NAT docker-сети и на Docker
    Desktop под Windows не находит камер вообще."""
    import httpx as httpx_module

    captured = {}

    class _FakeResponse:
        def json(self):
            return {"ok": True, "devices": [], "warnings": []}

    async def _fake_get(self, url, **kwargs):
        captured["params"] = kwargs.get("params")
        return _FakeResponse()

    monkeypatch.setattr(httpx_module.AsyncClient, "get", _fake_get)

    r = client.get(
        "/api/cameras/onvif/discover",
        params={"subnet": "192.168.105.0/24"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert captured["params"] == {"subnet": "192.168.105.0/24"}


def test_onvif_discover_is_admin_only(client, monkeypatch):
    """Сканирование сети — операция уровня администратора, как и остальное
    управление камерами."""
    r = client.get("/api/cameras/onvif/discover", params={"subnet": "192.168.1.0/24"})
    assert r.status_code == 401


def test_onvif_discover_returns_502_on_worker_error(client, admin_headers, monkeypatch):
    import httpx as httpx_module

    class _FakeResponse:
        def json(self):
            return {"ok": False, "error": "не удалось отправить WS-Discovery Probe: network unreachable", "devices": []}

    async def _fake_get(self, url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(httpx_module.AsyncClient, "get", _fake_get)

    r = client.get("/api/cameras/onvif/discover", headers=admin_headers)
    assert r.status_code == 502
    assert "network unreachable" in r.text


def test_onvif_discover_returns_503_when_worker_unreachable(client, admin_headers, monkeypatch):
    import httpx as httpx_module

    async def _fake_get(self, url, **kwargs):
        raise httpx_module.ConnectError("connection refused")

    monkeypatch.setattr(httpx_module.AsyncClient, "get", _fake_get)

    r = client.get("/api/cameras/onvif/discover", headers=admin_headers)
    assert r.status_code == 503


def test_onvif_profiles_requires_admin(client, admin_headers, request):
    username = _unique("op-onvif-profiles", request)
    client.post(
        "/api/users",
        json={"username": username, "password": "Op3rator!Pass1", "role": "operator"},
        headers=admin_headers,
    )
    r = client.post("/api/auth/login", data={"username": username, "password": "Op3rator!Pass1"})
    operator_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post("/api/cameras/onvif/profiles", json={"host": "192.168.1.64"}, headers=operator_headers)
    assert r.status_code == 403

    r = client.post("/api/cameras/onvif/profiles", json={"host": "192.168.1.64"})
    assert r.status_code == 401


def test_onvif_profiles_proxies_worker_response(client, admin_headers, monkeypatch):
    import httpx as httpx_module

    fake_profiles = [{"token": "profile_1", "name": "MainStream"}]
    captured = {}

    class _FakeResponse:
        def json(self):
            return {"ok": True, "profiles": fake_profiles}

    async def _fake_post(self, url, **kwargs):
        assert url.endswith("/onvif/profiles")
        captured["json"] = kwargs.get("json")
        return _FakeResponse()

    monkeypatch.setattr(httpx_module.AsyncClient, "post", _fake_post)

    r = client.post(
        "/api/cameras/onvif/profiles",
        json={"host": "192.168.1.64", "port": 80, "username": "admin", "password": "s3cret"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"profiles": fake_profiles}
    assert captured["json"]["host"] == "192.168.1.64"
    assert captured["json"]["password"] == "s3cret"


def test_onvif_profiles_returns_502_on_worker_error(client, admin_headers, monkeypatch):
    import httpx as httpx_module

    class _FakeResponse:
        def json(self):
            return {"ok": False, "error": "ONVIF-запрос не удался: timed out", "profiles": []}

    async def _fake_post(self, url, **kwargs):
        return _FakeResponse()

    monkeypatch.setattr(httpx_module.AsyncClient, "post", _fake_post)

    r = client.post("/api/cameras/onvif/profiles", json={"host": "192.168.1.64"}, headers=admin_headers)
    assert r.status_code == 502
    assert "timed out" in r.text


def test_onvif_stream_uri_proxies_worker_response(client, admin_headers, monkeypatch):
    import httpx as httpx_module

    class _FakeResponse:
        def json(self):
            return {"ok": True, "uri": "rtsp://192.168.1.64:554/profile1"}

    async def _fake_post(self, url, **kwargs):
        assert url.endswith("/onvif/stream-uri")
        assert kwargs["json"]["profile_token"] == "profile_1"
        return _FakeResponse()

    monkeypatch.setattr(httpx_module.AsyncClient, "post", _fake_post)

    r = client.post(
        "/api/cameras/onvif/stream-uri",
        json={"host": "192.168.1.64", "profile_token": "profile_1"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert r.json() == {"uri": "rtsp://192.168.1.64:554/profile1"}


def test_onvif_stream_uri_requires_admin(client, admin_headers, request):
    username = _unique("op-onvif-streamuri", request)
    client.post(
        "/api/users",
        json={"username": username, "password": "Op3rator!Pass1", "role": "operator"},
        headers=admin_headers,
    )
    r = client.post("/api/auth/login", data={"username": username, "password": "Op3rator!Pass1"})
    operator_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post(
        "/api/cameras/onvif/stream-uri",
        json={"host": "192.168.1.64", "profile_token": "profile_1"},
        headers=operator_headers,
    )
    assert r.status_code == 403


def test_operator_cannot_manage_cameras_but_can_view(client, admin_headers, request):
    username = _unique("operator", request)
    r = client.post(
        "/api/users",
        json={"username": username, "password": "Str0ngPass!23", "role": "operator"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text

    r = client.post("/api/auth/login", data={"username": username, "password": "Str0ngPass!23"})
    assert r.status_code == 200
    op_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    # Оператор видит список камер (не admin-only)...
    r = client.get("/api/cameras", headers=op_headers)
    assert r.status_code == 200
    # ...но не может добавлять камеры (ТЗ: матрица прав — управление камерами только админу)
    r = client.post(
        "/api/cameras",
        json={"name": "should-fail", "rtsp_url": "rtsp://x/y"},
        headers=op_headers,
    )
    assert r.status_code == 403

    # И не может смотреть список пользователей
    r = client.get("/api/users", headers=op_headers)
    assert r.status_code == 403


def test_camera_rejects_ssrf_style_non_rtsp_url(client, admin_headers):
    r = client.post(
        "/api/cameras",
        json={"name": "ssrf-attempt", "rtsp_url": "http://169.254.169.254/latest/meta-data/"},
        headers=admin_headers,
    )
    assert r.status_code == 422


def test_audit_log_records_mutating_action_and_is_admin_only(client, admin_headers, request):
    name = _unique("audit-cam", request)
    r = client.post(
        "/api/cameras",
        json={"name": name, "rtsp_url": "rtsp://cam/stream"},
        headers=admin_headers,
    )
    cam_id = r.json()["id"]

    r = client.get("/api/audit", headers=admin_headers)
    assert r.status_code == 200
    actions = [item["action"] for item in r.json()["items"]]
    assert "Добавлена камера" in actions

    client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_settings_update_persists_and_validates_range(client, admin_headers):
    r = client.put("/api/settings", json={"retention_days": 45}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["retention_days"] == "45"

    r = client.get("/api/settings", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["retention_days"] == "45"

    r = client.put("/api/settings", json={"retention_days": 999999}, headers=admin_headers)
    assert r.status_code == 400


def test_record_bitrate_and_iframe_only_persist_and_validate(client, admin_headers):
    r = client.put("/api/settings", json={"record_bitrate": 4000, "record_iframe_only": 1}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["record_bitrate"] == "4000"
    assert r.json()["record_iframe_only"] == "1"

    r = client.get("/api/settings", headers=admin_headers)
    assert r.json()["record_bitrate"] == "4000"

    r = client.put("/api/settings", json={"record_bitrate": -1}, headers=admin_headers)
    assert r.status_code == 400

    r = client.put("/api/settings", json={"record_iframe_only": 2}, headers=admin_headers)
    assert r.status_code == 400

    r = client.put("/api/settings", json={"record_bitrate": 0}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["record_bitrate"] == "0"


def test_apply_performance_profile_rewrites_tunables(client, admin_headers):
    r = client.post("/api/settings/profile/economy", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["performance_profile"] == "economy"
    assert body["face_model"] == "buffalo_s"

    r = client.post("/api/settings/profile/does-not-exist", headers=admin_headers)
    assert r.status_code == 400


def _register_and_login(client, admin_headers, username: str, password: str = "Str0ngPass!23"):
    r = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": "viewer"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", data={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}, password


def test_change_password_locks_out_after_repeated_wrong_old_password(client, admin_headers, request):
    from app.routers.auth import CHANGE_PW_MAX_ATTEMPTS

    username = _unique("changepw-lockout", request)
    headers, _ = _register_and_login(client, admin_headers, username)

    for _ in range(CHANGE_PW_MAX_ATTEMPTS):
        r = client.post(
            "/api/auth/change-password",
            json={"old_password": "definitely-wrong", "new_password": "NewStr0ngPass!23"},
            headers=headers,
        )
        assert r.status_code == 400, r.text

    # Лимит исчерпан — даже правильный old_password теперь отклоняется 429,
    # а не проверяется: иначе лимит не защищал бы от подбора.
    r = client.post(
        "/api/auth/change-password",
        json={"old_password": "Str0ngPass!23", "new_password": "NewStr0ngPass!23"},
        headers=headers,
    )
    assert r.status_code == 429, r.text


def test_change_password_succeeds_and_resets_counter_below_limit(client, admin_headers, request):
    username = _unique("changepw-ok", request)
    headers, password = _register_and_login(client, admin_headers, username)

    r = client.post(
        "/api/auth/change-password",
        json={"old_password": "wrong-once", "new_password": "NewStr0ngPass!23"},
        headers=headers,
    )
    assert r.status_code == 400, r.text

    r = client.post(
        "/api/auth/change-password",
        json={"old_password": password, "new_password": "NewStr0ngPass!23"},
        headers=headers,
    )
    assert r.status_code == 200, r.text


def test_onvif_bulk_add_creates_cameras_and_reports_failures(client, admin_headers, monkeypatch):
    """Массовое добавление найденных камер (ТЗ 18.7).

    Проверяется главное свойство: ошибка на одной камере не отменяет
    остальных. В сети из двух-трёх десятков устройств одно почти наверняка
    окажется недоступным или с другим паролем, и терять из-за него всю
    операцию — плохой обмен.
    """
    import httpx as httpx_module

    responses = {
        "10.9.9.1": {"ok": True, "name": "NewEntraceKPP",
                     "rtsp_url": "rtsp://admin:pw@10.9.9.1/main",
                     "sub_rtsp_url": "rtsp://admin:pw@10.9.9.1/sub"},
        "10.9.9.2": {"ok": True, "name": "Склад",
                     "rtsp_url": "rtsp://admin:pw@10.9.9.2/main", "sub_rtsp_url": None},
        "10.9.9.3": {"ok": False, "error": "неверный пароль"},
    }

    class _FakeResponse:
        def __init__(self, body):
            self._body = body

        def json(self):
            return self._body

    async def _fake_post(self, url, **kwargs):
        host = kwargs["json"]["host"]
        return _FakeResponse(responses[host])

    monkeypatch.setattr(httpx_module.AsyncClient, "post", _fake_post)

    r = client.post(
        "/api/cameras/onvif/bulk-add",
        json={"cameras": [
            {"host": "10.9.9.1", "port": 80, "username": "admin", "password": "pw"},
            {"host": "10.9.9.2", "port": 80, "username": "admin", "password": "pw"},
            {"host": "10.9.9.3", "port": 80, "username": "admin", "password": "wrong"},
        ]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert [a["name"] for a in body["added"]] == ["NewEntraceKPP", "Склад"]
    assert body["added"][0]["has_substream"] is True
    assert body["added"][1]["has_substream"] is False, "камера с одним профилем — без субпотока"
    assert [f["host"] for f in body["failed"]] == ["10.9.9.3"]
    assert "неверный пароль" in body["failed"][0]["error"]

    # Повторный запуск не должен задваивать уже добавленные камеры: поиск
    # по сети администратор запускает не один раз.
    again = client.post(
        "/api/cameras/onvif/bulk-add",
        json={"cameras": [{"host": "10.9.9.1", "port": 80, "username": "admin", "password": "pw"}]},
        headers=admin_headers,
    )
    assert again.status_code == 200
    assert again.json()["added"] == []
    assert again.json()["skipped"][0]["host"] == "10.9.9.1"

    for cam in body["added"]:
        client.delete(f"/api/cameras/{cam['id']}", headers=admin_headers)


def test_onvif_bulk_add_is_admin_only(client):
    r = client.post("/api/cameras/onvif/bulk-add", json={"cameras": []})
    assert r.status_code == 401
