from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import User
from ..auth import verify_password, create_token, get_current_user, hash_password
from ..schemas import Token, UserOut, PasswordChange
from ..services.pubsub import get_redis

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_ATTEMPTS = 10        # попыток за окно
WINDOW_SEC = 300         # окно блокировки, сек


@router.post("/login", response_model=Token)
async def login(request: Request, form: OAuth2PasswordRequestForm = Depends(), db: AsyncSession = Depends(get_db)):
    redis = get_redis()
    ip = request.client.host if request.client else "unknown"
    key = f"login_fail:{ip}:{form.username}"
    try:
        attempts = int(await redis.get(key) or 0)
    except Exception:
        attempts = 0
    if attempts >= MAX_ATTEMPTS:
        raise HTTPException(status_code=429, detail="Слишком много попыток. Повторите через несколько минут.")

    r = await db.execute(select(User).where(User.username == form.username))
    user = r.scalar_one_or_none()
    if not user or not verify_password(form.password, user.password_hash):
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
    return Token(access_token=token, role=user.role, username=user.username)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return user


@router.post("/change-password")
async def change_password(
    payload: PasswordChange,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(400, "Старый пароль неверен")
    user.password_hash = hash_password(payload.new_password)
    await db.commit()
    return {"ok": True}
