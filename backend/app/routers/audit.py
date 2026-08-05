import io
import csv
from datetime import datetime
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..db import get_db
from ..models import AuditLog
from ..auth import require_role, require_role_query
from ..pagination import PageParams


router = APIRouter(prefix="/api/audit", tags=["audit"])


def _base_query(username: str | None, action: str | None, date_from: datetime | None, date_to: datetime | None):
    q = select(AuditLog)
    if username:
        q = q.where(AuditLog.username == username)
    if action:
        q = q.where(AuditLog.action.ilike(f"%{action}%"))
    if date_from:
        q = q.where(AuditLog.ts >= date_from)
    if date_to:
        q = q.where(AuditLog.ts <= date_to)
    return q


@router.get("")
async def list_audit(
    username: str | None = None,
    action: str | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    page: PageParams = Depends(),
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    base = _base_query(username, action, date_from, date_to)
    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.order_by(AuditLog.ts.desc()).limit(page.page_size).offset(page.offset)
    )).scalars().all()
    return {
        "items": [
            {
                "id": r.id, "ts": r.ts.isoformat() if r.ts else None,
                "username": r.username, "role": r.role,
                "action": r.action, "method": r.method, "path": r.path,
                "status_code": r.status_code, "ip": r.ip,
            }
            for r in rows
        ],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
    }


# --- Экспорт (token в query, чтобы открывалось как обычная ссылка) -----------

# Выгрузки открываются браузером по прямой ссылке (токен в query string),
# но роль берётся из БД, а не из claim'а токена: журнал аудита — самые
# чувствительные данные системы, и разжалованный администратор не должен
# выгружать его целиком ещё 30 минут до истечения access-токена.
_require_audit_export = require_role_query("admin")


async def _all_rows(db: AsyncSession, username, action, date_from, date_to, hard_limit: int = 10000):
    q = _base_query(username, action, date_from, date_to).order_by(AuditLog.ts.desc()).limit(hard_limit)
    return (await db.execute(q)).scalars().all()


HEADER = ["Время", "Пользователь", "Роль", "Действие", "Метод", "Путь", "Код", "IP"]


def _row_tuple(r):
    return [r.ts.isoformat() if r.ts else "", r.username, r.role, r.action, r.method, r.path, r.status_code, r.ip or ""]


@router.get("/export.csv")
async def export_csv(
    _=Depends(_require_audit_export),
    username: str | None = None, action: str | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None,
    db: AsyncSession = Depends(get_db),
):
    rows = await _all_rows(db, username, action, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(HEADER)
    for r in rows:
        w.writerow(_row_tuple(r))
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=audit.csv"})


@router.get("/export.xlsx")
async def export_xlsx(
    _=Depends(_require_audit_export),
    username: str | None = None, action: str | None = None,
    date_from: datetime | None = None, date_to: datetime | None = None,
    db: AsyncSession = Depends(get_db),
):
    rows = await _all_rows(db, username, action, date_from, date_to)
    wb = Workbook()
    ws = wb.active
    ws.title = "Аудит"
    ws.append(HEADER)
    for r in rows:
        ws.append(_row_tuple(r))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf,
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=audit.xlsx"})
