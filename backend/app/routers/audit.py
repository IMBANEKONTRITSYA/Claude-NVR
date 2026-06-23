from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..db import get_db
from ..models import AuditLog
from ..auth import require_role

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
async def list_audit(
    limit: int = 200,
    username: str | None = None,
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    q = select(AuditLog).order_by(AuditLog.ts.desc()).limit(limit)
    if username:
        q = q.where(AuditLog.username == username)
    rows = (await db.execute(q)).scalars().all()
    return [
        {
            "id": r.id, "ts": r.ts.isoformat() if r.ts else None,
            "username": r.username, "role": r.role,
            "action": r.action, "method": r.method, "path": r.path,
            "status_code": r.status_code, "ip": r.ip,
        }
        for r in rows
    ]
