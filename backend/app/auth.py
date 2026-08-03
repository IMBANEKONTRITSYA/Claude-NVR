import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update
from .config import settings
from .db import get_db
from .models import User, RefreshToken

pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/login")

ROLES = ("admin", "operator", "viewer")


def hash_password(p: str) -> str:
    return pwd_ctx.hash(p)


def verify_password(p: str, h: str) -> bool:
    return pwd_ctx.verify(p, h)


def create_token(sub: str, role: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({"sub": sub, "role": role, "exp": exp}, settings.SECRET_KEY, algorithm="HS256")


def _hash_refresh_token(raw: str) -> str:
    # sha256, не bcrypt: это не пароль, а высокоэнтропийный случайный токен
    # (secrets.token_urlsafe) — нужен быстрый детерминированный поиск по
    # хэшу в БД, а не защита от подбора по словарю.
    return hashlib.sha256(raw.encode()).hexdigest()


async def create_refresh_token(db: AsyncSession, user_id: int) -> str:
    raw = secrets.token_urlsafe(48)
    expires = datetime.now(timezone.utc) + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    db.add(RefreshToken(user_id=user_id, token_hash=_hash_refresh_token(raw), expires_at=expires))
    await db.commit()
    return raw


async def rotate_refresh_token(db: AsyncSession, raw_token: str) -> tuple[User, str] | None:
    """Проверяет refresh-токен, отзывает его и выдаёт новый (ротация —
    ТЗ 13). Если предъявлен токен, уже отмеченный отозванным — это признак
    кражи/повторного использования (кто-то ещё владеет старым токеном):
    отзываем ВСЕ токены пользователя, чтобы прервать сессию похитителя."""
    token_hash = _hash_refresh_token(raw_token)
    r = await db.execute(select(RefreshToken).where(RefreshToken.token_hash == token_hash))
    rt = r.scalar_one_or_none()
    if not rt:
        return None

    now = datetime.now(timezone.utc)
    expires_at = rt.expires_at if rt.expires_at.tzinfo else rt.expires_at.replace(tzinfo=timezone.utc)
    if rt.revoked_at is not None:
        # Уже потрачен легитимной ротацией — кто-то предъявляет старую копию
        # токена, которая больше не должна существовать: кража. Отзыв через
        # logout/смену пароля/revoke-all — ожидаемый повторный отказ, без
        # эскалации на остальные сессии.
        if rt.rotated:
            await revoke_all_user_tokens(db, rt.user_id)
        return None
    if expires_at < now:
        return None

    ru = await db.execute(select(User).where(User.id == rt.user_id))
    user = ru.scalar_one_or_none()
    if not user:
        return None

    rt.revoked_at = now
    rt.rotated = True
    await db.commit()
    new_raw = await create_refresh_token(db, user.id)
    return user, new_raw


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> None:
    token_hash = _hash_refresh_token(raw_token)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def revoke_all_user_tokens(db: AsyncSession, user_id: int) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(timezone.utc))
    )
    await db.commit()


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    cred_exc = HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Не авторизован")
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        if not username:
            raise cred_exc
    except JWTError:
        raise cred_exc
    r = await db.execute(select(User).where(User.username == username))
    user = r.scalar_one_or_none()
    if not user:
        raise cred_exc
    return user


def require_role(*roles: str):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return checker
