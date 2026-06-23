from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from ..db import get_db
from ..models import User
from ..auth import require_role, hash_password
from ..schemas import UserCreate, UserOut

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("", response_model=list[UserOut])
async def list_users(_=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(User).order_by(User.id))
    return r.scalars().all()


@router.post("", response_model=UserOut)
async def create_user(payload: UserCreate, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    exists = (await db.execute(select(User).where(User.username == payload.username))).scalar_one_or_none()
    if exists:
        raise HTTPException(400, "Пользователь уже существует")
    u = User(username=payload.username, password_hash=hash_password(payload.password), role=payload.role)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@router.delete("/{user_id}")
async def delete_user(user_id: int, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
    return {"ok": True}
