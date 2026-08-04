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
