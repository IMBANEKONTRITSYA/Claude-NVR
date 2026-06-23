import json
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, func
from ..db import get_db
from ..models import Person, FaceEvent
from ..auth import require_role
from ..schemas import PersonOut, PersonUpdate
from ..pagination import PageParams
from ..services.pubsub import get_redis

router = APIRouter(prefix="/api/persons", tags=["persons"])


@router.get("")
async def list_persons(
    status: str | None = None,
    q: str | None = None,
    page: PageParams = Depends(),
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    base = select(Person)
    if status:
        base = base.where(Person.status == status)
    if q:
        base = base.where(Person.name.ilike(f"%{q}%"))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.order_by(Person.id.desc()).limit(page.page_size).offset(page.offset)
    )).scalars().all()
    return {
        "items": [
            {
                "id": p.id, "name": p.name, "status": p.status,
                "avatar_path": p.avatar_path,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
    }


@router.get("/{pid}", response_model=PersonOut)
async def get_person(pid: int, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    p = await db.get(Person, pid)
    if not p:
        raise HTTPException(404, "Персона не найдена")
    return p


@router.patch("/{pid}", response_model=PersonOut)
async def update_person(pid: int, payload: PersonUpdate, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    p = await db.get(Person, pid)
    if not p:
        raise HTTPException(404, "Персона не найдена")
    if payload.name is not None:
        p.name = payload.name
        if payload.name.strip():
            p.status = "known"
    if payload.status is not None:
        p.status = payload.status
    await db.commit()
    await db.refresh(p)
    return p


@router.delete("/{pid}")
async def delete_person(pid: int, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Person).where(Person.id == pid))
    await db.commit()
    return {"ok": True}


@router.post("/{src_id}/merge/{dst_id}")
async def merge_persons(src_id: int, dst_id: int, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    if src_id == dst_id:
        raise HTTPException(400, "Нельзя слить с самой собой")
    await db.execute(update(FaceEvent).where(FaceEvent.person_id == src_id).values(person_id=dst_id))
    await db.execute(delete(Person).where(Person.id == src_id))
    await db.commit()
    return {"ok": True}


@router.get("/{pid}/gallery", response_model=list[dict])
async def gallery(pid: int, limit: int = 50, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(FaceEvent.id, FaceEvent.ts, FaceEvent.snapshot_path, FaceEvent.camera_id, FaceEvent.enhanced)
        .where(FaceEvent.person_id == pid)
        .order_by(FaceEvent.ts.desc())
        .limit(limit)
    )
    return [{"id": x[0], "ts": x[1], "snapshot_path": x[2], "camera_id": x[3], "enhanced": x[4]} for x in r.all()]


@router.post("/{pid}/enhance")
async def enhance_person(pid: int, limit: int = 20, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    """Принудительный нейросетевой апскейл снимков персоны (ставит в очередь)."""
    person = await db.get(Person, pid)
    if not person:
        raise HTTPException(404, "Персона не найдена")
    r = await db.execute(
        select(FaceEvent.id).where(FaceEvent.person_id == pid).order_by(FaceEvent.ts.desc()).limit(limit)
    )
    ids = [x[0] for x in r.all()]
    redis = get_redis()
    for eid in ids:
        await redis.lpush("upscale:queue", json.dumps({"event_id": eid, "force": True}))
    return {"ok": True, "queued": len(ids)}
