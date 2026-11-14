"""/api/cameras/{id}/snapshot: снимок по запросу не открыт для перебора (D04).

Снимок у камер без аналитики снимается ffmpeg'ом с RTSP MediaMTX
(services/live_preview.py). Номер камеры сверяется с БД до снимка: иначе
любой вошедший пользователь, перебирая номера, запускал бы ffmpeg на
каждый — отрицательный кэш привязан к номеру и перебор обходит.
"""
from app.services import live_preview


def test_unknown_camera_is_404_without_running_ffmpeg(client, admin_token, monkeypatch):
    calls = []

    async def fake_grab(cam_id):
        calls.append(cam_id)
        return False

    monkeypatch.setattr(live_preview, "_grab", fake_grab)
    r = client.get("/api/cameras/987654/snapshot", params={"token": admin_token})
    assert r.status_code == 404
    assert "не найдена" in r.json()["detail"]
    assert calls == [], "снимок пытались снять у несуществующей камеры"


def test_existing_camera_without_frame_is_404_and_tried_once(client, admin_token, make_camera, monkeypatch):
    cam_id = make_camera("snapshot-probe-cam")["id"]
    calls = []

    async def fake_grab(cid):
        calls.append(cid)
        return False

    monkeypatch.setattr(live_preview, "_grab", fake_grab)
    monkeypatch.setattr(live_preview, "_failed_at", {})
    for _ in range(3):
        r = client.get(f"/api/cameras/{cam_id}/snapshot", params={"token": admin_token})
        assert r.status_code == 404
    assert calls == [cam_id], f"ffmpeg запускался {len(calls)} раз — отказ не закэширован"


def test_snapshot_requires_token(client):
    assert client.get("/api/cameras/1/snapshot").status_code in (401, 403)
