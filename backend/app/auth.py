import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Query, status
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


def _utcnow_naive() -> datetime:
    """RefreshToken.expires_at/revoked_at — DateTime без timezone (TIMESTAMP
    WITHOUT TIME ZONE в Postgres, значения по конвенции — UTC). asyncpg (в
    отличие от aiosqlite, которым пользуются юнит-тесты) отказывается
    биндить timezone-aware datetime в такую колонку — эта функция для
    именно записи в БД; при чтении naive-значение по-прежнему трактуется
    как UTC (см. rotate_refresh_token)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def hash_password(p: str) -> str:
    return pwd_ctx.hash(p)


def verify_password(p: str, h: str) -> bool:
    return pwd_ctx.verify(p, h)


async def verify_password_async(p: str, h: str) -> bool:
    """bcrypt — намеренно медленная CPU-bound функция (сотни мс на
    текущем work factor). Вызов синхронного verify_password() напрямую
    внутри async-обработчика блокирует единственный event loop uvicorn на
    всё это время: не только сам /login, но и вообще все остальные
    корутины (снепшоты живой сетки, дашборд, архив у уже залогиненных
    пользователей) встают в очередь за ним, пока bcrypt считается в том
    же потоке. Обнаружено нагрузочным тестом (loadtest/locustfile.py,
    SPEC.md §14 "Нагрузочное тестирование... проверка стабильности FPS и
    задержек"): при 16 одновременных клиентах p99 задержки на никак не
    связанных с логином эндпоинтах подскакивал до 300-900мс именно в
    моменты конкурентных входов — под SPEC.md "задержка трансляции
    ≤3 сек" и "24/7 без деградации" это реальный риск на пересменке, когда
    несколько операторов логинятся почти одновременно. asyncio.to_thread
    не ускоряет сам bcrypt (это by-design медленная функция — защита от
    brute-force), но переносит блокировку в пул потоков, освобождая
    event loop для остальных запросов."""
    return await asyncio.to_thread(verify_password, p, h)


async def hash_password_async(p: str) -> str:
    """См. verify_password_async — тот же bcrypt-cost, тот же риск
    блокировки event loop, применяется при регистрации/смене пароля."""
    return await asyncio.to_thread(hash_password, p)


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
    expires = _utcnow_naive() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
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

    rt.revoked_at = now.replace(tzinfo=None)
    rt.rotated = True
    await db.commit()
    new_raw = await create_refresh_token(db, user.id)
    return user, new_raw


async def revoke_refresh_token(db: AsyncSession, raw_token: str) -> None:
    token_hash = _hash_refresh_token(raw_token)
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == token_hash, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_utcnow_naive())
    )
    await db.commit()


async def revoke_all_user_tokens(db: AsyncSession, user_id: int) -> None:
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=_utcnow_naive())
    )
    await db.commit()


async def user_from_token(token: str, db: AsyncSession) -> User:
    """Разбирает access-токен и возвращает пользователя ИЗ БД.

    Единственное место, где токен превращается в личность: и заголовочный
    путь (get_current_user), и «ссылочный» (get_user_from_query_token)
    проходят здесь, поэтому подпись, наличие `sub` и существование учётной
    записи проверяются для обоих одинаково.
    """
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


async def get_current_user(token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)) -> User:
    return await user_from_token(token, db)


async def get_user_from_query_token(
    token: str = Query(..., description="JWT access-токен"),
    db: AsyncSession = Depends(get_db),
) -> User:
    """То же, что get_current_user, но токен берётся из query string.

    Часть эндпоинтов открывается браузером как обычная ссылка (медиа-файлы,
    выгрузки отчётов и аудита, скачивание сегмента архива, Prometheus,
    WebSocket'ы) — там нет возможности поставить заголовок Authorization,
    поэтому токен передаётся параметром `?token=`. Способ доставки токена —
    единственное, что отличает эти эндпоинты от остальных; проверки должны
    быть теми же.

    Раньше каждый такой обработчик звал `jwt.decode` сам и брал роль прямо
    из claim'а, не заглядывая в БД. Токен подписан, так что подделать роль
    было нельзя, но claim фиксирует состояние на момент выдачи, а не
    текущее: до истечения access-токена (30 минут по умолчанию) удалённый
    пользователь продолжал скачивать записи архива, медиа-файлы и выгрузки,
    а разжалованный из admin в viewer — экспортировать журнал аудита
    целиком. На эндпоинтах с `require_role` то же самое отсекалось сразу,
    потому что get_current_user ходит в БД за пользователем и его ролью, —
    расхождение выходило не в дизайне, а в том, что проверка была написана
    руками в обход общей.
    """
    return await user_from_token(token, db)


def require_role(*roles: str):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return checker


def require_role_query(*roles: str):
    """require_role для эндпоинтов с токеном в query string.

    Роль сверяется с `User.role` из БД, а не с claim'ом токена, — см.
    get_user_from_query_token.
    """
    async def checker(user: User = Depends(get_user_from_query_token)) -> User:
        if user.role not in roles:
            raise HTTPException(status_code=403, detail="Недостаточно прав")
        return user
    return checker
