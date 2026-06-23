import io
import csv
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from jose import jwt, JWTError
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..config import settings
from ..db import get_db
from ..models import FaceEvent, Person, Camera


def _check_token(token: str):
    try:
        p = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
        if p.get("role") not in ("admin", "operator"):
            raise HTTPException(403, "Недостаточно прав")
    except JWTError:
        raise HTTPException(401, "Не авторизован")

router = APIRouter(prefix="/api/reports", tags=["reports"])


async def _appearances(db: AsyncSession, days: int):
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(FaceEvent.id, FaceEvent.ts, Camera.name, Person.id, Person.name, Person.status)
        .join(Camera, Camera.id == FaceEvent.camera_id)
        .outerjoin(Person, Person.id == FaceEvent.person_id)
        .where(FaceEvent.ts >= since)
        .order_by(FaceEvent.ts.desc())
    )
    return r.all()


@router.get("/appearances.csv")
async def appearances_csv(days: int = 7, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    _check_token(token)
    rows = await _appearances(db, days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID события", "Время", "Камера", "ID персоны", "Имя", "Статус"])
    for x in rows:
        w.writerow([x[0], x[1].isoformat(), x[2], x[3] or "", x[4] or "Неизвестный", x[5] or ""])
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=appearances.csv"})


@router.get("/appearances.xlsx")
async def appearances_xlsx(days: int = 7, token: str = Query(...), db: AsyncSession = Depends(get_db)):
    _check_token(token)
    rows = await _appearances(db, days)
    wb = Workbook()
    ws = wb.active
    ws.title = "Появления"
    ws.append(["ID события", "Время", "Камера", "ID персоны", "Имя", "Статус"])
    for x in rows:
        ws.append([x[0], x[1].isoformat(), x[2], x[3] or "", x[4] or "Неизвестный", x[5] or ""])
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": "attachment; filename=appearances.xlsx"})
