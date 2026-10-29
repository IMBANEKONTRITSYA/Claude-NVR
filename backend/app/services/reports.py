"""Построение отчётов (SPEC §8: «Экспорт в Excel/CSV: события, активность
по камерам, статистика. Настраиваемые шаблоны отчётов. Автоматическая
отправка по расписанию (email)»).

Выборки и сборка файла вынесены из `routers/reports.py`, потому что теперь
у них два потребителя: скачивание по ссылке из браузера и планировщик,
который кладёт тот же файл во вложение письма. Общий код тут — не ради
устранения дублирования как такового, а ради гарантии, что отчёт,
пришедший на почту, совпадает со скачанным вручную: разойдись они, никто
бы этого не заметил до первого разбора расхождения в цифрах.
"""
import csv
import io
from datetime import datetime, timedelta

from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Camera, FaceEvent, Person
from . import csv_export
from .display_timezone import format_local, load_display_timezone, zone_header

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def _appearances(db: AsyncSession, days: int, tz: str):
    since = datetime.utcnow() - timedelta(days=days)
    r = await db.execute(
        select(FaceEvent.id, FaceEvent.ts, Camera.name, Person.id, Person.name, Person.status)
        .join(Camera, Camera.id == FaceEvent.camera_id)
        .outerjoin(Person, Person.id == FaceEvent.person_id)
        .where(FaceEvent.ts >= since)
        .order_by(FaceEvent.ts.desc())
    )
    return [[x[0], format_local(x[1], tz), x[2], x[3] or "", x[4] or "Неизвестный", x[5] or ""]
            for x in r.all()]


async def _persons_summary(db: AsyncSession, days: int, tz: str):
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
    return [[x[0], x[1] or "Неизвестный", x[2] or "", x[3],
             format_local(x[4], tz), format_local(x[5], tz)]
            for x in r.all()]


async def _cameras_activity(db: AsyncSession, days: int, tz: str):
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
    return [[x[0], x[1], x[2] or "", x[3], x[4]] for x in r.all()]


# Виды отчётов: {ключ: (человеческое название, заголовки, выборка, лист)}.
# Ключ уходит в БД (report_schedules.kind) и в URL, поэтому переименование
# ключа — миграция, а не косметика.
KINDS: dict[str, tuple] = {
    "appearances": (
        "Появления",
        ["ID события", "Время", "Камера", "ID персоны", "Имя", "Статус"],
        _appearances, "Появления",
    ),
    "persons": (
        "Сводка по персонам",
        ["ID", "Имя", "Статус", "Появлений", "Первое", "Последнее"],
        _persons_summary, "Персоны",
    ),
    "cameras": (
        "Активность по камерам",
        ["ID", "Камера", "Локация", "Обнаружений", "Уникальных персон"],
        _cameras_activity, "Камеры",
    ),
}

FORMATS = ("csv", "xlsx")

# Столбцы со временем: в файле они в поясе объекта, и пояс дописывается в
# заголовок — см. services/display_timezone.py.
TIME_COLUMNS = frozenset({"Время", "Первое", "Последнее"})


def header_for(header: list[str], tz: str) -> list[str]:
    return [zone_header(h, tz) if h in TIME_COLUMNS else h for h in header]


def kind_title(kind: str) -> str:
    return KINDS[kind][0] if kind in KINDS else kind


def to_csv(header: list[str], rows) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for row in rows:
        w.writerow(row)
    # Кодировка — общая для всех выгрузок системы: BOM, иначе Excel под
    # Windows читает кириллические заголовки отчёта как cp1251. Раньше
    # здесь стоял простой `.encode("utf-8")` с оговоркой «чинить надо
    # разом для всех выгрузок, иначе будет разнобой» — разнобой к этому
    # моменту уже был (выгрузка камер §3 писала BOM), поэтому все три
    # выгрузки сведены в `csv_export.encode`. Обоснование — там же.
    return csv_export.encode(buf.getvalue())


def to_xlsx(title: str, header: list[str], rows) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = title
    ws.append(header)
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def build_report(db: AsyncSession, kind: str, fmt: str, days: int) -> tuple[str, bytes, str]:
    """Готовый файл отчёта: `(имя файла, содержимое, MIME)`.

    Один вход и для скачивания, и для планировщика — см. докстринг модуля.
    """
    if kind not in KINDS:
        raise ValueError(f"неизвестный вид отчёта: {kind}")
    if fmt not in FORMATS:
        raise ValueError(f"неизвестный формат: {fmt}")
    _, header, query, sheet = KINDS[kind]
    tz = await load_display_timezone(db)
    rows = await query(db, days, tz)
    header = header_for(header, tz)
    if fmt == "csv":
        return f"{kind}.csv", to_csv(header, rows), "text/csv"
    return f"{kind}.xlsx", to_xlsx(sheet, header, rows), XLSX_MIME
