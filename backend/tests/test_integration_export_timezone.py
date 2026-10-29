"""Выгрузки отчётов и журнала аудита пишут время в поясе объекта (живая БД)."""
import csv
import io

import pytest

EKB = "Asia/Yekaterinburg"


@pytest.fixture
def site_zone(client, admin_headers):
    before = client.get("/api/settings/client", headers=admin_headers).json().get("display_timezone")
    r = client.put("/api/settings", headers=admin_headers, json={"display_timezone": EKB})
    assert r.status_code == 200, r.text
    yield EKB
    client.put("/api/settings", headers=admin_headers, json={"display_timezone": before or "UTC"})


@pytest.fixture
def seeded_event(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('tz-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id")
        cam_id = cur.fetchone()[0]
        # Время события задано явно в UTC: проверяется именно перевод.
        cur.execute(
            "INSERT INTO face_events (camera_id, ts, is_known, enhanced) "
            "VALUES (%s, date_trunc('second', timezone('utc', now())) - interval '1 hour', false, false) "
            "RETURNING ts", (cam_id,))
        ts = cur.fetchone()[0]
    yield cam_id, ts
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM face_events WHERE camera_id = %s", (cam_id,))
        cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))


def _rows(resp):
    return list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))


def test_appearances_csv_in_site_zone(client, admin_token, site_zone, seeded_event):
    from app.services.display_timezone import format_local

    _, ts = seeded_event
    r = client.get("/api/reports/appearances.csv", params={"token": admin_token, "days": 1})
    assert r.status_code == 200
    rows = _rows(r)
    assert rows[0][1] == f"Время ({EKB})"
    times = {row[1] for row in rows[1:] if row[2] == "tz-cam"}
    assert times == {format_local(ts, EKB)}
    assert ts.strftime("%Y-%m-%d %H:%M:%S") not in times, "время осталось в UTC"


def test_appearances_xlsx_in_site_zone(client, admin_token, site_zone, seeded_event):
    from openpyxl import load_workbook

    from app.services.display_timezone import format_local

    _, ts = seeded_event
    r = client.get("/api/reports/appearances.xlsx", params={"token": admin_token, "days": 1})
    ws = load_workbook(io.BytesIO(r.content)).active
    rows = list(ws.iter_rows(values_only=True))
    assert rows[0][1] == f"Время ({EKB})"
    assert {row[1] for row in rows[1:] if row[2] == "tz-cam"} == {format_local(ts, EKB)}


def test_audit_csv_in_site_zone(client, admin_token, site_zone, pg_conn):
    from app.services.display_timezone import format_local

    r = client.get("/api/audit/export.csv", params={"token": admin_token})
    assert r.status_code == 200
    rows = _rows(r)
    assert rows[0][0] == f"Время ({EKB})"
    with pg_conn.cursor() as cur:
        # Несколько последних: запрос выгрузки сам может попасть в журнал.
        cur.execute("SELECT ts FROM audit_log ORDER BY ts DESC LIMIT 5")
        recent = {format_local(t, EKB) for (t,) in cur.fetchall()}
    assert rows[1][0] in recent
