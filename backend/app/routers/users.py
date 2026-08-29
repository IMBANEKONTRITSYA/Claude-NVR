from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from ..db import get_db
from ..models import User
from ..auth import require_role, hash_password_async
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
    u = User(username=payload.username, password_hash=await hash_password_async(payload.password), role=payload.role)
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


@router.delete("/{user_id}")
async def delete_user(user_id: int, current: User = Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    # Без первой проверки админ мог удалить сам себя (self-lockout —
    # текущая сессия и refresh-токены остаются валидны до истечения, но
    # войти заново или управлять системой после logout уже нельзя, и
    # main.py:lifespan досеивает "admin" только если такого username вообще
    # не существует — переименованный/другой-по-имени последний админ не
    # восстановится автоматически при перезапуске). Это единственный
    # реалистичный путь к нулю администраторов через этот эндпоинт: он сам
    # требует роль admin, поэтому если удаляющий и удаляемый — разные
    # пользователи, админов на момент проверки как минимум два (оба
    # существуют одновременно), и подсчёт ниже не сработает. Второй чек
    # (подсчёт) — оставлен как defense-in-depth и явная фиксация инварианта
    # «в системе всегда есть хотя бы один администратор» на случай будущего
    # изменения self-check выше или появления другого пути удаления.
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(404, "Пользователь не найден")
    if target.id == current.id:
        raise HTTPException(400, "Нельзя удалить собственную учётную запись")
    if target.role == "admin":
        admin_count = (await db.execute(
            select(func.count()).select_from(User).where(User.role == "admin")
        )).scalar() or 0
        if admin_count <= 1:
            raise HTTPException(400, "Нельзя удалить последнего администратора")
    await db.execute(delete(User).where(User.id == user_id))
    await db.commit()
    return {"ok": True}
