"""P1: /ws/faces и /ws/cameras проверяли токен один раз при подключении
(app/routers/ws.py::_auth) и дальше держали сокет открытым сколько угодно —
истечение 30-минутного access-токена или удаление пользователя не закрывали
уже открытое соединение, в отличие от REST (get_current_user перепроверяет
на каждый запрос). `_still_valid` вызывается на каждом цикле пинга (~30с) и
даёт те же гарантии на реальной (in-memory SQLite) БД — только таблица
users, без pgvector-полей других моделей."""
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.config import settings
from app.auth import hash_password
from app.db import Base
from app.models import User
from app.routers.ws import _still_valid

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[User.__table__])
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        yield session
    await engine.dispose()


@pytest.fixture
async def user(db_session):
    u = User(username="operator1", password_hash=hash_password("x"), role="operator")
    db_session.add(u)
    await db_session.commit()
    await db_session.refresh(u)
    return u


def _token(username: str, minutes: float) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return jwt.encode({"sub": username, "role": "operator", "exp": exp}, settings.SECRET_KEY, algorithm="HS256")


async def test_valid_token_and_existing_user_passes(db_session, user):
    token = _token(user.username, minutes=30)
    assert await _still_valid(token, db_session) is True


async def test_expired_token_fails(db_session, user):
    token = _token(user.username, minutes=-1)
    assert await _still_valid(token, db_session) is False


async def test_deleted_user_fails(db_session, user):
    token = _token(user.username, minutes=30)
    await db_session.delete(user)
    await db_session.commit()
    assert await _still_valid(token, db_session) is False


async def test_garbage_token_fails(db_session, user):
    assert await _still_valid("not-a-jwt", db_session) is False


async def test_token_without_sub_fails(db_session, user):
    exp = datetime.now(timezone.utc) + timedelta(minutes=30)
    token = jwt.encode({"role": "operator", "exp": exp}, settings.SECRET_KEY, algorithm="HS256")
    assert await _still_valid(token, db_session) is False
