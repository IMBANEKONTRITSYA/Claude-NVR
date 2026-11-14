"""Интеграционные тесты /ws/faces и /ws/cameras: авторизация по
query-параметру ?token= (WebSocket не может слать заголовки на этапе
handshake из браузера) и доставка Redis pub/sub сообщений подписчику."""
import json
import time

import pytest
import redis as redis_sync
from starlette.websockets import WebSocketDisconnect

from app.config import settings


def test_ws_faces_rejects_invalid_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/faces?token=not-a-jwt"):
            pass


def test_ws_faces_rejects_missing_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/faces"):
            pass


def test_ws_faces_delivers_published_message(client, admin_token):
    with client.websocket_connect(f"/ws/faces?token={admin_token}") as ws:
        time.sleep(0.3)  # даём приложению время выполнить pubsub.subscribe до publish
        r = redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
        payload = json.dumps({"marker": "test-face-event"})
        for _ in range(5):
            r.publish("faces:new", payload)
            time.sleep(0.1)

        received = None
        for _ in range(5):
            msg = ws.receive_text()
            if "test-face-event" in msg:
                received = msg
                break
        assert received is not None


def test_ws_cameras_delivers_published_message(client, admin_token):
    with client.websocket_connect(f"/ws/cameras?token={admin_token}") as ws:
        time.sleep(0.3)
        r = redis_sync.from_url(settings.REDIS_URL, decode_responses=True)
        payload = json.dumps({"marker": "test-camera-status"})
        for _ in range(5):
            r.publish("cameras:status", payload)
            time.sleep(0.1)

        received = None
        for _ in range(5):
            msg = ws.receive_text()
            if "test-camera-status" in msg:
                received = msg
                break
        assert received is not None
