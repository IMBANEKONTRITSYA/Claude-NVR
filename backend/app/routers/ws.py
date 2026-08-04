import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query, HTTPException
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..config import settings
from ..db import SessionLocal
from ..models import User
from ..services.pubsub import get_redis

router = APIRouter()


def _auth(token: str) -> dict:
    try:
        return jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Не авторизован")


async def _still_valid(token: str, db: AsyncSession) -> bool:
    """REST-эндпоинты перепроверяют токен (истечение + существование
    пользователя в БД) на каждый запрос через get_current_user. WS-ручки
    раньше проверяли токен один раз при подключении и держали соединение
    открытым сколько угодно — истечение 30-минутного access-токена,
    удаление пользователя или смена пароля не закрывали уже открытый
    сокет. Вызывается на каждом цикле пинга (~30с), чтобы дать те же
    гарантии, что и REST."""
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return False
    username = payload.get("sub")
    if not username:
        return False
    r = await db.execute(select(User).where(User.username == username))
    return r.scalar_one_or_none() is not None


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
                async with SessionLocal() as db:
                    if not await _still_valid(token, db):
                        await ws.close(code=4401)
                        break
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
                async with SessionLocal() as db:
                    if not await _still_valid(token, db):
                        await ws.close(code=4401)
                        break
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe("cameras:status")
        await pubsub.close()
