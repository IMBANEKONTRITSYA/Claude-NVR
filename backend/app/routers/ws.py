import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from jose import jwt, JWTError
from ..config import settings
from ..services.pubsub import get_redis

router = APIRouter()


def _auth(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Не авторизован")


@router.websocket("/ws/faces")
async def ws_faces(ws: WebSocket, token: str = Query(...)):
    _auth(token)
    await ws.accept()
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("faces:new", "faces:enhanced")
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
            if msg and msg.get("type") == "message":
                await ws.send_text(msg["data"])
            else:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe("faces:new", "faces:enhanced")
        await pubsub.close()


@router.websocket("/ws/cameras")
async def ws_cameras(ws: WebSocket, token: str = Query(...)):
    _auth(token)
    await ws.accept()
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("cameras:status")
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
            if msg and msg.get("type") == "message":
                await ws.send_text(msg["data"])
            else:
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe("cameras:status")
        await pubsub.close()
