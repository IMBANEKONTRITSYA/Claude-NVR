import io
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from ..auth import require_role, require_role_query
from ..db import get_db
from ..params import days_param
from ..models import ReportSchedule
from ..services import report_scheduler
from ..services.reports import (
    FORMATS,
    KINDS,
    XLSX_MIME,
    build_report,
    to_csv,
    to_xlsx,
)

# Выгрузки открываются браузером по прямой ссылке, поэтому токен идёт в
# query string, а не в заголовке. Роль сверяется с БД (require_role_query),
# а не с claim'ом токена: разжалованный из operator в viewer не должен
# выгружать отчёты по всем событиям ещё 30 минут до истечения токена.
_require_report_access = require_role_query("admin", "operator")

router = APIRouter(prefix="/api/reports", tags=["reports"])


def _download(filename: str, payload: bytes, mime: str) -> StreamingResponse:
    return StreamingResponse(
        io.BytesIO(payload), media_type=mime,
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# Прежде на каждый вид отчёта было по два почти одинаковых обработчика
# (csv + xlsx), шесть штук на три вида. Пути сохранены буква в букву:
# ссылки на выгрузки лежат в интерфейсе и в закладках операторов, а сам
# набор видов теперь один и тот же и для скачивания, и для планировщика
# (services/reports.py: KINDS).
def _register_download(kind: str, default_days: int) -> None:
    for fmt in FORMATS:
        async def handler(days: int = days_param(default_days),
                          db: AsyncSession = Depends(get_db),
                          _=Depends(_require_report_access),
                          _kind: str = kind, _fmt: str = fmt):
            filename, payload, mime = await build_report(db, _kind, _fmt, days)
            return _download(filename, payload, mime)

        router.add_api_route(f"/{kind}.{fmt}", handler, methods=["GET"],
                             name=f"{kind}_{fmt}")


_register_download("appearances", 7)
_register_download("persons", 30)
_register_download("cameras", 30)


# --- Шаблоны отчётов и расписание (SPEC §8) ---------------------------------

class ScheduleIn(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=120)
    kind: str
    fmt: str = "xlsx"
    days: int = Field(default=7, ge=1, le=3650)
    recipients: str = Field(default="", max_length=500)
    enabled: bool = False
    period: str = "daily"
    hour: int = Field(default=8, ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    day_of_week: int = Field(default=0, ge=0, le=6)
    # 28, а не 31: расписание «31-го числа» молча не сработало бы в
    # феврале, и заметили бы это через месяцы (см. models.ReportSchedule).
    day_of_month: int = Field(default=1, ge=1, le=28)


def _validate(payload: ScheduleIn) -> None:
    if payload.kind not in KINDS:
        raise HTTPException(400, f"Неизвестный вид отчёта: {payload.kind}")
    if payload.fmt not in FORMATS:
        raise HTTPException(400, f"Неизвестный формат: {payload.fmt}")
    if payload.period not in report_scheduler.PERIODS:
        raise HTTPException(400, f"Неизвестный период: {payload.period}")


def _out(s: ReportSchedule) -> dict:
    return {
        "id": s.id, "name": s.name, "kind": s.kind, "fmt": s.fmt,
        "days": s.days, "recipients": s.recipients, "enabled": s.enabled,
        "period": s.period, "hour": s.hour, "minute": s.minute,
        "day_of_week": s.day_of_week, "day_of_month": s.day_of_month,
        "last_sent_at": s.last_sent_at.isoformat() if s.last_sent_at else None,
        "last_error": s.last_error,
    }


@router.get("/kinds")
async def list_kinds(_=Depends(require_role("admin", "operator"))):
    """Виды отчётов и форматы — чтобы интерфейс не держал свою копию
    списка, которая разъедется с бэкендом при добавлении вида."""
    return {
        "kinds": [{"key": k, "title": v[0]} for k, v in KINDS.items()],
        "formats": list(FORMATS),
        "periods": list(report_scheduler.PERIODS),
    }


@router.get("/schedules")
async def list_schedules(_=Depends(require_role("admin", "operator")),
                         db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ReportSchedule).order_by(ReportSchedule.id))).scalars().all()
    return [_out(s) for s in rows]


@router.post("/schedules")
async def create_schedule(payload: ScheduleIn,
                          _=Depends(require_role("admin")),
                          db: AsyncSession = Depends(get_db)):
    _validate(payload)
    sched = ReportSchedule(**payload.model_dump())
    db.add(sched)
    await db.commit()
    await db.refresh(sched)
    return _out(sched)


@router.put("/schedules/{sched_id}")
async def update_schedule(sched_id: int, payload: ScheduleIn,
                          _=Depends(require_role("admin")),
                          db: AsyncSession = Depends(get_db)):
    _validate(payload)
    sched = await db.get(ReportSchedule, sched_id)
    if not sched:
        raise HTTPException(404, "Шаблон не найден")
    for key, val in payload.model_dump().items():
        setattr(sched, key, val)
    # Правка расписания открывает слот заново: администратор, сдвинувший
    # время с 20:00 на 08:00, ждёт отчёт завтра в 8, а не «уже отправляли
    # сегодня в 20». Ошибку прошлой попытки тоже сбрасываем — она про
    # прежние параметры.
    sched.last_sent_at = None
    sched.last_error = None
    await db.commit()
    return _out(sched)


@router.delete("/schedules/{sched_id}")
async def delete_schedule(sched_id: int,
                          _=Depends(require_role("admin")),
                          db: AsyncSession = Depends(get_db)):
    sched = await db.get(ReportSchedule, sched_id)
    if not sched:
        raise HTTPException(404, "Шаблон не найден")
    await db.delete(sched)
    await db.commit()
    return {"ok": True}


@router.post("/schedules/{sched_id}/send")
async def send_now(sched_id: int,
                   _=Depends(require_role("admin", "operator")),
                   db: AsyncSession = Depends(get_db)):
    """Отправить отчёт немедленно, не дожидаясь слота.

    Нужна и как проверка настроек («дойдёт ли вообще»), и как ручной
    запуск шаблона без расписания. `last_sent_at` намеренно НЕ трогается:
    ручная отправка не должна закрывать запланированный слот, иначе
    проверка кнопкой отменяла бы утренний отчёт.
    """
    sched = await db.get(ReportSchedule, sched_id)
    if not sched:
        raise HTTPException(404, "Шаблон не найден")
    cfg = await report_scheduler._smtp_config(db)
    try:
        ok = await report_scheduler.send_scheduled_report(db, sched, cfg)
    except Exception as e:
        raise HTTPException(502, f"Не удалось отправить: {e}")
    if not ok:
        raise HTTPException(400, "Почта не настроена: заполните SMTP и получателей")
    return {"ok": True}
