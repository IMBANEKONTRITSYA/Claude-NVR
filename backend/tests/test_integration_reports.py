"""Интеграционные тесты /api/reports/* (CSV/XLSX-экспорт). Эти эндпоинты
не используют Bearer-заголовок (они открываются напрямую в браузере/по
ссылке для скачивания) — авторизация идёт через ?token=, поэтому RBAC и
валидация токена здесь проверяются отдельно от остальных роутеров."""


def _insert_camera_person_event(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('rep-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO persons (name, status, alert_on_detection, created_at) "
            "VALUES ('Отчётная персона', 'known', false, NOW()) RETURNING id"
        )
        person_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, is_known, enhanced) "
            "VALUES (%s, %s, NOW(), true, false)",
            (cam_id, person_id),
        )
    return cam_id, person_id


def test_reports_missing_token_rejected(client):
    r = client.get("/api/reports/appearances.csv")
    assert r.status_code == 422


def test_reports_invalid_token_rejected(client):
    r = client.get("/api/reports/appearances.csv", params={"token": "not-a-jwt"})
    assert r.status_code == 401


def test_reports_viewer_role_forbidden(client, make_user, admin_token, request):
    _, viewer_token = make_user(f"viewer_{request.node.name}"[:60], "viewer")

    r = client.get("/api/reports/appearances.csv", params={"token": viewer_token})
    assert r.status_code == 403


def test_appearances_csv_contains_seeded_row(client, admin_token, pg_conn):
    _insert_camera_person_event(pg_conn)
    r = client.get("/api/reports/appearances.csv", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "Отчётная персона" in r.text


def test_appearances_xlsx_downloads_with_correct_content_type(client, admin_token, pg_conn):
    _insert_camera_person_event(pg_conn)
    r = client.get("/api/reports/appearances.xlsx", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers["content-type"]
    assert len(r.content) > 0


def test_persons_summary_csv(client, admin_token, pg_conn):
    _insert_camera_person_event(pg_conn)
    r = client.get("/api/reports/persons.csv", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    assert "Отчётная персона" in r.text


def test_persons_summary_xlsx(client, admin_token, pg_conn):
    _insert_camera_person_event(pg_conn)
    r = client.get("/api/reports/persons.xlsx", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    assert len(r.content) > 0


def test_cameras_activity_csv(client, admin_token, pg_conn):
    _insert_camera_person_event(pg_conn)
    r = client.get("/api/reports/cameras.csv", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    assert "rep-cam" in r.text


def test_cameras_activity_xlsx(client, admin_token, pg_conn):
    _insert_camera_person_event(pg_conn)
    r = client.get("/api/reports/cameras.xlsx", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    assert len(r.content) > 0
