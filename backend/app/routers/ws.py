import asyncio
import json
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Query
from jose import jwt, JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from ..config import settings
from ..db import SessionLocal
from ..models import User
from ..services.pubsub import get_redis
from ..services.face_feed_acl import filter_face_message

router = APIRouter()


async def _auth(ws: WebSocket, token: str) -> str | None:
    """Проверка на этапе рукопожатия: подпись токена И существование
    пользователя в БД. Возвращает **роль** из БД или None при отказе.

    Проверять только подпись было недостаточно: удалённый пользователь
    открывал новый сокет и получал поток событий до первого цикла пинга
    (~30 секунд), хотя REST-эндпоинты отказывали ему сразу. Теперь вход и
    удержание соединения (_current_role) проверяют одно и то же.

    Роль возвращается, а не проверяется здесь, потому что от неё зависит
    не допуск, а **состав** сообщений ленты лиц (см. face_feed_acl).

    Закрытие вместо HTTPException: до accept() Starlette превращает close в
    отказ рукопожатия, и клиент видит обычный WebSocketDisconnect.
    """
    async with SessionLocal() as db:
        role = await _current_role(token, db)
    if role is not None:
        return role
    await ws.close(code=4401)
    return None


async def _current_role(token: str, db: AsyncSession) -> str | None:
    """Актуальная роль предъявителя токена из БД, либо None.

    REST-эндпоинты перепроверяют токен (истечение + существование
    пользователя в БД) на каждый запрос через get_current_user. WS-ручки
    раньше проверяли токен один раз при подключении и держали соединение
    открытым сколько угодно — истечение 30-минутного access-токена,
    удаление пользователя или смена пароля не закрывали уже открытый
    сокет. Вызывается на каждом цикле пинга (~30с), чтобы дать те же
    гарантии, что и REST.

    Роль читается из БД на каждом цикле, а не запоминается на входе:
    разжалованный из оператора в наблюдатели должен перестать получать
    снимки и теги на **уже открытом** сокете — ровно та гарантия, которую
    `require_role` даёт на REST (роль сверяется с БД, а не с claim'ом
    токена, см. auth.py).
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return None
    username = payload.get("sub")
    if not username:
        return None
    r = await db.execute(select(User).where(User.username == username))
    user = r.scalar_one_or_none()
    return user.role if user is not None else None


async def _still_valid(token: str, db: AsyncSession) -> bool:
    """Совместимая обёртка над _current_role для сокетов, которым роль не
    нужна (статусы камер разрешены всем ролям строкой «Просмотр видео
    онлайн» §18)."""
    return await _current_role(token, db) is not None


@router.websocket("/ws/faces")
async def ws_faces(ws: WebSocket, token: str = Query(...)):
    role = await _auth(ws, token)
    if role is None:
        return
    await ws.accept()
    r = get_redis()
    pubsub = r.pubsub()
    await pubsub.subscribe("faces:new", "faces:enhanced")
    try:
        while True:
            msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=30)
            if msg and msg.get("type") == "message":
                # Состав сообщения зависит от роли: наблюдателю уходит
                # только геометрия рамки для живого оверлея §4, без снимка
                # и watchlist-разметки, которые §18 закрывает строкой
                # «Карточки персон». См. services/face_feed_acl.py.
                out = filter_face_message(msg["data"], role)
                if out is not None:
                    await ws.send_text(out)
            else:
                async with SessionLocal() as db:
                    fresh = await _current_role(token, db)
                if fresh is None:
                    await ws.close(code=4401)
                    break
                role = fresh
                await ws.send_text(json.dumps({"type": "ping"}))
    except WebSocketDisconnect:
        pass
    finally:
        await pubsub.unsubscribe("faces:new", "faces:enhanced")
        await pubsub.close()


@router.websocket("/ws/cameras")
async def ws_cameras(ws: WebSocket, token: str = Query(...)):
    if not await _auth(ws, token):
        return
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
