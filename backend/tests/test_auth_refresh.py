"""ТЗ 13: "JWT-токены с refresh-механизмом". Проверяет ротацию и отзыв
refresh-токенов на реальной (in-memory SQLite) БД — только таблицы users/
refresh_tokens создаются напрямую, без pgvector-полей других моделей."""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.auth import (
    create_refresh_token,
    rotate_refresh_token,
    revoke_refresh_token,
    revoke_all_user_tokens,
    hash_password,
)
from app.db import Base
from app.models import User, RefreshToken

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all, tables=[User.__table__, RefreshToken.__table__])
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


async def test_refresh_token_stored_hashed_not_raw(db_session, user):
    raw = await create_refresh_token(db_session, user.id)
    from sqlalchemy import select

    rt = (await db_session.execute(select(RefreshToken))).scalar_one()
    assert rt.token_hash != raw
    assert rt.revoked_at is None


async def test_rotation_issues_new_token_and_revokes_old(db_session, user):
    raw1 = await create_refresh_token(db_session, user.id)
    result = await rotate_refresh_token(db_session, raw1)
    assert result is not None
    rotated_user, raw2 = result
    assert rotated_user.id == user.id
    assert raw2 != raw1

    # Старый токен использовать повторно нельзя.
    again = await rotate_refresh_token(db_session, raw1)
    assert again is None


async def test_reuse_of_rotated_token_revokes_all_user_tokens(db_session, user):
    """Кто-то предъявляет уже провёрнутый (значит — украденный) refresh-токен.
    Это должно инвалидировать вообще все токены пользователя, включая тот,
    что был выдан легитимной ротацией — иначе похититель и жертва оба
    продолжают доступ."""
    raw1 = await create_refresh_token(db_session, user.id)
    _, raw2 = await rotate_refresh_token(db_session, raw1)

    # Повторное предъявление старого (уже отозванного) raw1 — сигнал кражи.
    stolen_attempt = await rotate_refresh_token(db_session, raw1)
    assert stolen_attempt is None

    # Токен легитимного пользователя (raw2), выданный при ротации, тоже
    # должен быть отозван — revoke_all перекрыл всю сессию.
    legit_attempt = await rotate_refresh_token(db_session, raw2)
    assert legit_attempt is None


async def test_expired_token_rejected(db_session, user):
    from datetime import datetime, timedelta

    from app.auth import _hash_refresh_token

    raw = "already-expired-token"
    db_session.add(RefreshToken(
        user_id=user.id,
        token_hash=_hash_refresh_token(raw),
        expires_at=datetime.utcnow() - timedelta(days=1),
    ))
    await db_session.commit()

    assert await rotate_refresh_token(db_session, raw) is None


async def test_unknown_token_rejected(db_session, user):
    assert await rotate_refresh_token(db_session, "never-issued") is None


async def test_logout_revokes_single_token(db_session, user):
    raw1 = await create_refresh_token(db_session, user.id)
    raw2 = await create_refresh_token(db_session, user.id)

    await revoke_refresh_token(db_session, raw1)

    assert await rotate_refresh_token(db_session, raw1) is None
    # Второй токен (другая сессия/устройство) не затронут.
    result = await rotate_refresh_token(db_session, raw2)
    assert result is not None


async def test_revoke_all_user_tokens_invalidates_every_session(db_session, user):
    raw1 = await create_refresh_token(db_session, user.id)
    raw2 = await create_refresh_token(db_session, user.id)

    await revoke_all_user_tokens(db_session, user.id)

    assert await rotate_refresh_token(db_session, raw1) is None
    assert await rotate_refresh_token(db_session, raw2) is None
