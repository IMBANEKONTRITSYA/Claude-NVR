from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..config import settings
from ..db import get_db
from ..models import User
from ..auth import (
    verify_password_async,
    create_token,
    get_current_user,
    hash_password_async,
    create_refresh_token,
    rotate_refresh_token,
    revoke_refresh_token,
    revoke_all_user_tokens,
)
from ..schemas import Token, UserOut, PasswordChange, RefreshRequest, LogoutRequest
from ..services.pubsub import get_redis

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_ATTEMPTS = 10        # попыток за окно
WINDOW_SEC = 300         # окно блокировки, сек

CHANGE_PW_MAX_ATTEMPTS = 5   # попыток неверного old_password за окно
CHANGE_PW_WINDOW_SEC = 300


def real_ip(request: Request) -> str:
    """За nginx request.client.host — это адрес прокси; читаем X-Real-IP."""
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _is_password_expired(user: User) -> bool:
    changed_at = user.password_changed_at
    if changed_at is None:
        return False
    if changed_at.tzinfo is None:
        changed_at = changed_at.replace(tzinfo=timezone.utc)
    age = datetime.now(timezone.utc) - changed_at
    return age > timedelta(days=settings.PASSWORD_MAX_AGE_DAYS)


@router.post("/login", response_model=Token)
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    redis = get_redis()
    ip = real_ip(request)
    key = f"login_fail:{ip}:{form.username}"
    try:
        attempts = int(await redis.get(key) or 0)
    except Exception:
        attempts = 0
    if attempts >= MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Повторите через несколько минут.")

    r = await db.execute(select(User).where(User.username == form.username))
    user = r.scalar_one_or_none()
    if not user or not await verify_password_async(form.password, user.password_hash):
        try:
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, WINDOW_SEC)
            await pipe.execute()
        except Exception:
            pass
        raise HTTPException(status_code=401, detail="Неверный логин или пароль")

    try:
        await redis.delete(key)
    except Exception:
        pass
    token = create_token(user.username, user.role)
    refresh_token = await create_refresh_token(db, user.id)
    return Token(
        access_token=token,
        refresh_token=refresh_token,
        role=user.role,
        username=user.username,
        password_expired=_is_password_expired(user),
    )


@router.post("/refresh", response_model=Token)
async def refresh(payload: RefreshRequest, db: AsyncSession = Depends(get_db)):
    result = await rotate_refresh_token(db, payload.refresh_token)
    if not result:
        raise HTTPException(status_code=401, detail="Refresh-токен недействителен или истёк")
    user, new_refresh_token = result
    access_token = create_token(user.username, user.role)
    return Token(
        access_token=access_token,
        refresh_token=new_refresh_token,
        role=user.role,
        username=user.username,
        password_expired=_is_password_expired(user),
    )


@router.post("/logout")
async def logout(payload: LogoutRequest, db: AsyncSession = Depends(get_db)):
    # Не требует аутентификации access-токеном: клиент может вызывать logout
    # именно потому, что access-токен уже истёк, имея на руках только
    # refresh-токен. Сам refresh-токен и есть предъявляемый секрет.
    await revoke_refresh_token(db, payload.refresh_token)
    return {"ok": True}


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password")
async def change_password(
    payload: PasswordChange,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Без этого лимита обладатель уже похищенного/недолговечного access-
    # токена мог подбирать old_password неограниченное число раз — токен
    # короткоживущий (30 мин), но подобранный пароль остаётся рабочим
    # секретом навсегда. У /login есть такой же лимит по IP+логину; здесь —
    # по user.id, потому что владение валидным access-токеном уже сильнее
    # идентифицирует атакующего, чем IP (токен можно использовать с любого).
    redis = get_redis()
    key = f"changepw_fail:{user.id}"
    try:
        attempts = int(await redis.get(key) or 0)
    except Exception:
        attempts = 0
    if attempts >= CHANGE_PW_MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Повторите через несколько минут.")

    if not await verify_password_async(payload.old_password, user.password_hash):
        try:
            pipe = redis.pipeline()
            pipe.incr(key)
            pipe.expire(key, CHANGE_PW_WINDOW_SEC)
            await pipe.execute()
        except Exception:
            pass
        raise HTTPException(400, "Старый пароль неверен")

    try:
        await redis.delete(key)
    except Exception:
        pass
    user.password_hash = await hash_password_async(payload.new_password)
    # naive UTC: users.password_changed_at — TIMESTAMP WITHOUT TIME ZONE,
    # asyncpg отказывается биндить timezone-aware datetime в такую колонку
    # (тот же паттерн, что ../auth.py:_utcnow_naive для refresh_tokens).
    user.password_changed_at = datetime.now(timezone.utc).replace(tzinfo=None)
    # Смена пароля — сигнал "эта учётка могла быть скомпрометирована":
    # отзываем все refresh-токены, вынуждая перелогиниться везде, включая
    # устройство злоумышленника, если пароль сменили именно поэтому.
    await revoke_all_user_tokens(db, user.id)
    await db.commit()
    return {"ok": True}
