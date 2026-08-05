import io
import csv
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func
from ..auth import require_role_query
from ..db import get_db
from ..models import FaceEvent, Person, Camera

# Выгрузки открываются браузером по прямой ссылке, поэтому токен идёт в
# query string, а не в заголовке. Роль сверяется с БД (require_role_query),
# а не с claim'ом токена: разжалованный из operator в viewer не должен
# выгружать отчёты по всем событиям ещё 30 минут до истечения токена.
_require_report_access = require_role_query("admin", "operator")

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
async def appearances_csv(days: int = 7, db: AsyncSession = Depends(get_db), _=Depends(_require_report_access)):
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
async def appearances_xlsx(days: int = 7, db: AsyncSession = Depends(get_db), _=Depends(_require_report_access)):
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


# --- Сводка по персонам -----------------------------------------------------

async def _persons_summary(db: AsyncSession, days: int):
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(
            Person.id, Person.name, Person.status,
            func.count(FaceEvent.id).label("cnt"),
            func.min(FaceEvent.ts), func.max(FaceEvent.ts),
        )
        .join(FaceEvent, FaceEvent.person_id == Person.id)
        .where(FaceEvent.ts >= since)
        .group_by(Person.id)
        .order_by(func.count(FaceEvent.id).desc())
    )
    return r.all()


def _stream_csv(filename: str, header: list[str], rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow(row)
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


def _stream_xlsx(filename: str, title: str, header: list[str], rows):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(header)
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f"attachment; filename={filename}"})


@router.get("/persons.csv")
async def persons_csv(days: int = 30, db: AsyncSession = Depends(get_db), _=Depends(_require_report_access)):
    rows = await _persons_summary(db, days)
    data = [[x[0], x[1] or "Неизвестный", x[2] or "", x[3],
             x[4].isoformat() if x[4] else "", x[5].isoformat() if x[5] else ""] for x in rows]
    return _stream_csv("persons.csv", ["ID", "Имя", "Статус", "Появлений", "Первое", "Последнее"], data)


@router.get("/persons.xlsx")
async def persons_xlsx(days: int = 30, db: AsyncSession = Depends(get_db), _=Depends(_require_report_access)):
    rows = await _persons_summary(db, days)
    data = [[x[0], x[1] or "Неизвестный", x[2] or "", x[3],
             x[4].isoformat() if x[4] else "", x[5].isoformat() if x[5] else ""] for x in rows]
    return _stream_xlsx("persons.xlsx", "Персоны", ["ID", "Имя", "Статус", "Появлений", "Первое", "Последнее"], data)


# --- Активность по камерам --------------------------------------------------

async def _cameras_activity(db: AsyncSession, days: int):
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(
            Camera.id, Camera.name, Camera.location,
            func.count(FaceEvent.id).label("cnt"),
            func.count(func.distinct(FaceEvent.person_id)),
        )
        .outerjoin(FaceEvent, (FaceEvent.camera_id == Camera.id) & (FaceEvent.ts >= since))
        .group_by(Camera.id)
        .order_by(func.count(FaceEvent.id).desc())
    )
    return r.all()


@router.get("/cameras.csv")
async def cameras_csv(days: int = 30, db: AsyncSession = Depends(get_db), _=Depends(_require_report_access)):
    rows = await _cameras_activity(db, days)
    data = [[x[0], x[1], x[2] or "", x[3], x[4]] for x in rows]
    return _stream_csv("cameras.csv", ["ID", "Камера", "Локация", "Обнаружений", "Уникальных персон"], data)


@router.get("/cameras.xlsx")
async def cameras_xlsx(days: int = 30, db: AsyncSession = Depends(get_db), _=Depends(_require_report_access)):
    rows = await _cameras_activity(db, days)
    data = [[x[0], x[1], x[2] or "", x[3], x[4]] for x in rows]
    return _stream_xlsx("cameras.xlsx", "Камеры", ["ID", "Камера", "Локация", "Обнаружений", "Уникальных персон"], data)
