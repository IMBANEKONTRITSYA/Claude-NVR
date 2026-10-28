"""Время с поясом во входных параметрах — выдача, а не 500.

DEPLOY_FINDINGS D11 (развёртывание 2026-10-28): `POST /api/search/face`
с `date_from=2026-10-28T00:00:00+05:00` отвечал 500 — asyncpg не может
сравнить aware-datetime с `TIMESTAMP WITHOUT TIME ZONE`. Тем же дефектом
оказались больны журнал аудита, его выгрузка и поиск сегментов архива —
причём архив падал и на голом `Z`, то есть на любом ISO 8601-времени
внешнего клиента §12.

Проверяется не только «не 500»: строка засевается в известный момент UTC,
и окно, выраженное в +05:00, обязано включать её с одной стороны и
исключать с другой. Нормализация, отбрасывающая пояс без пересчёта
(`replace(tzinfo=None)`), прошла бы проверку на 500 и сдвинула бы окно на
пять часов — здесь она падает.
"""
from datetime import datetime

import httpx
import pytest

from tests.test_integration_search import _fake_worker_post, _vec

# Момент события в UTC и два окна вокруг него, записанные в +05:00:
# 16:00+05:00 = 11:00Z (раньше события), 18:00+05:00 = 13:00Z (позже).
EVENT_UTC = datetime(2026, 10, 28, 12, 0, 0)
BEFORE = "2026-10-28T16:00:00+05:00"
AFTER = "2026-10-28T18:00:00+05:00"
MARKER = "tz-aware-probe"


@pytest.fixture()
def seeded(pg_conn):
    ids = {}
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES (%s, 'unused-enc-blob', '', false, 'offline', NOW()) RETURNING id",
            (MARKER,),
        )
        ids["cam"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO persons (name, status, alert_on_detection, created_at) "
            "VALUES (%s, 'known', false, NOW()) RETURNING id",
            (MARKER,),
        )
        ids["person"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, enhanced) "
            "VALUES (%s, %s, %s, CAST(%s AS vector), true, false)",
            (ids["cam"], ids["person"], EVENT_UTC, _vec()),
        )
        cur.execute(
            "INSERT INTO video_segments (camera_id, started_at, ended_at, file_path, "
            "event_type, duration_sec, size_bytes) "
            "VALUES (%s, %s, %s, '/nonexistent/tz-probe.mp4', 'continuous', 60, 0) RETURNING id",
            (ids["cam"], EVENT_UTC, EVENT_UTC.replace(minute=1)),
        )
        ids["segment"] = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO audit_log (ts, username, role, action, method, path, status_code) "
            "VALUES (%s, %s, 'admin', %s, 'GET', '/probe', 200) RETURNING id",
            (EVENT_UTC, MARKER, MARKER),
        )
        ids["audit"] = cur.fetchone()[0]
    try:
        yield ids
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE id = %s", (ids["audit"],))
            cur.execute("DELETE FROM video_segments WHERE id = %s", (ids["segment"],))
            cur.execute("DELETE FROM face_events WHERE camera_id = %s", (ids["cam"],))
            cur.execute("DELETE FROM persons WHERE id = %s", (ids["person"],))
            cur.execute("DELETE FROM cameras WHERE id = %s", (ids["cam"],))


def _segment_ids(client, headers, cam_id, **params):
    r = client.get("/api/archive/segments", params={"camera_id": cam_id, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return {s["id"] for s in r.json()}


def _audit_ids(client, headers, **params):
    r = client.get("/api/audit", params={"username": MARKER, **params}, headers=headers)
    assert r.status_code == 200, r.text
    return {row["id"] for row in r.json()["items"]}


def _search_hits(client, headers, cam_id, **data):
    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        data={"threshold": "0.5", **data},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    return [item for item in r.json() if item.get("camera_id") == cam_id]


@pytest.mark.parametrize("bound", [BEFORE, "2026-10-28T11:00:00Z"])
def test_archive_segments_accept_offset_and_z(client, admin_headers, seeded, bound):
    assert seeded["segment"] in _segment_ids(client, admin_headers, seeded["cam"], date_from=bound)


def test_archive_segments_window_is_converted_not_truncated(client, admin_headers, seeded):
    """18:00+05:00 — это 13:00Z, сегмент 12:00Z за пределами окна.

    Отбрось нормализация пояс без пересчёта, окно стало бы «от 18:00Z» —
    результат тот же, — поэтому парная проверка ниже идёт с другой стороны.
    """
    assert seeded["segment"] not in _segment_ids(client, admin_headers, seeded["cam"], date_from=AFTER)
    assert seeded["segment"] not in _segment_ids(client, admin_headers, seeded["cam"], date_to=BEFORE)
    # 16:00+05:00 как date_to — 11:00Z, раньше сегмента; а голый «16:00»
    # был бы позже. Именно эта пара отличает пересчёт от отбрасывания.
    assert seeded["segment"] in _segment_ids(client, admin_headers, seeded["cam"], date_to=AFTER)


def test_audit_list_accepts_offset(client, admin_headers, seeded):
    assert seeded["audit"] in _audit_ids(client, admin_headers, date_from=BEFORE)
    assert seeded["audit"] not in _audit_ids(client, admin_headers, date_from=AFTER)
    assert seeded["audit"] not in _audit_ids(client, admin_headers, date_to=BEFORE)


def test_audit_export_accepts_offset(client, admin_headers, seeded):
    for fmt in ("export.csv", "export.xlsx"):
        r = client.get(f"/api/audit/{fmt}", params={"username": MARKER, "date_from": BEFORE},
                       headers=admin_headers)
        assert r.status_code == 200, f"{fmt}: {r.status_code} {r.text[:200]}"


def test_face_search_accepts_offset(client, admin_headers, seeded, monkeypatch):
    """Ровно воспроизведение D11 из DEPLOY_FINDINGS_2026-10-28.md."""
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_worker_post)
    assert _search_hits(client, admin_headers, seeded["cam"], date_from=BEFORE)
    assert not _search_hits(client, admin_headers, seeded["cam"], date_from=AFTER)
    assert not _search_hits(client, admin_headers, seeded["cam"], date_to=BEFORE)


def test_naive_input_keeps_its_utc_meaning(client, admin_headers, seeded):
    """Naive-время по контракту API — UTC; нормализация его не трогает."""
    assert seeded["segment"] in _segment_ids(client, admin_headers, seeded["cam"], date_from="2026-10-28T11:00:00")
    assert seeded["segment"] not in _segment_ids(client, admin_headers, seeded["cam"], date_from="2026-10-28T13:00:00")
