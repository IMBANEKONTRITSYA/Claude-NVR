"""Системный мониторинг (SPEC §14): метрики хоста, состояние сервисов,
хранилище архива (§21), Prometheus."""
import json
import shutil
from datetime import datetime, timedelta

import psutil
from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal, get_db
from ..models import Camera, FaceEvent, Setting, VideoSegment
from ..auth import require_role, require_role_query
from ..services.pubsub import get_redis
from ..services.storage import (BYTES_PER_GB, DISK_CRIT_PCT, DISK_WARN_PCT,
                                calibration, days_left, disk_alert_level,
                                nominal_gb_per_day, required_gb)

# SPEC §1: «Основной поток: H.265, 1280×720 @ 15 fps, 2048 kbps» — базовая
# фактическая конфигурация камер объекта. Номинальный расход считается от
# неё; см. пояснение в storage_report(), почему это константа, а не настройка.
MAIN_STREAM_KBPS = 2048

router = APIRouter(prefix="/api/system", tags=["system"])


async def _collect() -> dict:
    """Единый сбор метрик — используется и JSON-эндпоинтом, и Prometheus."""
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    try:
        disk = shutil.disk_usage(settings.MEDIA_PATH)
        disk_total, disk_used, disk_free = disk.total, disk.used, disk.free
    except OSError:
        disk_total = disk_used = disk_free = 0

    # Температура доступна не на всех платформах (в Docker под Windows — нет)
    temp = None
    try:
        sensors = psutil.sensors_temperatures() or {}
        for entries in sensors.values():
            if entries:
                temp = entries[0].current
                break
    except (AttributeError, OSError):
        pass

    async with SessionLocal() as db:
        cams = (await db.execute(select(Camera))).scalars().all()
        online = sum(1 for c in cams if c.status == "online")
        enabled = sum(1 for c in cams if c.enabled)
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        events_today = (await db.execute(
            select(func.count(FaceEvent.id)).where(FaceEvent.ts >= today)
        )).scalar() or 0
        segments = (await db.execute(select(func.count(VideoSegment.id)))).scalar() or 0

    redis_ok = True
    queue_len = 0
    fps: dict[str, float] = {}
    try:
        r = get_redis()
        await r.ping()
        queue_len = await r.llen("upscale:queue")
        # Воркер публикует свой FPS по каждой камере в хеш worker:fps
        raw = await r.hgetall("worker:fps")
        fps = {k: float(v) for k, v in (raw or {}).items()}
    except Exception:
        redis_ok = False

    return {
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_mb": round(mem.used / 1048576),
        "ram_total_mb": round(mem.total / 1048576),
        "temperature_c": temp,
        "disk_total_gb": round(disk_total / 1073741824, 1),
        "disk_used_gb": round(disk_used / 1073741824, 1),
        "disk_free_gb": round(disk_free / 1073741824, 1),
        "disk_percent": round(disk_used / disk_total * 100, 1) if disk_total else 0,
        "cameras_total": len(cams),
        "cameras_enabled": enabled,
        "cameras_online": online,
        "events_today": events_today,
        "segments_total": segments,
        "upscale_queue": queue_len,
        "redis_ok": redis_ok,
        "camera_fps": fps,
    }


@router.get("/metrics")
async def system_metrics(_=Depends(require_role("admin", "operator"))):
    """Метрики для админ-дашборда. Оператору доступно ограниченно (см. матрицу прав)."""
    return await _collect()


@router.get("/prometheus")
async def prometheus_metrics(_=Depends(require_role_query("admin"))):
    """Экспорт в формате Prometheus (ТЗ 12: интеграция с Prometheus + Grafana).

    Скрейперы не умеют слать Bearer-заголовок, поэтому токен передаётся
    в query — как и для остальных «ссылочных» эндпоинтов (отчёты, медиа).
    Роль при этом сверяется с БД, а не берётся из claim'а: учётка, заведённая
    для скрейпера и затем удалённая или разжалованная, должна терять доступ
    к метрикам сразу, а не по истечении access-токена.
    """
    m = await _collect()
    lines = [
        "# HELP facewatch_cpu_percent Загрузка CPU, %",
        "# TYPE facewatch_cpu_percent gauge",
        f"facewatch_cpu_percent {m['cpu_percent']}",
        "# HELP facewatch_ram_percent Использование RAM, %",
        "# TYPE facewatch_ram_percent gauge",
        f"facewatch_ram_percent {m['ram_percent']}",
        "# HELP facewatch_disk_percent Заполнение диска архива, %",
        "# TYPE facewatch_disk_percent gauge",
        f"facewatch_disk_percent {m['disk_percent']}",
        "# HELP facewatch_disk_free_gb Свободно на диске архива, ГБ",
        "# TYPE facewatch_disk_free_gb gauge",
        f"facewatch_disk_free_gb {m['disk_free_gb']}",
        "# HELP facewatch_cameras_online Камер в сети",
        "# TYPE facewatch_cameras_online gauge",
        f"facewatch_cameras_online {m['cameras_online']}",
        "# HELP facewatch_cameras_enabled Камер включено",
        "# TYPE facewatch_cameras_enabled gauge",
        f"facewatch_cameras_enabled {m['cameras_enabled']}",
        "# HELP facewatch_events_today Событий распознавания за сегодня",
        "# TYPE facewatch_events_today counter",
        f"facewatch_events_today {m['events_today']}",
        "# HELP facewatch_upscale_queue Длина очереди апскейла",
        "# TYPE facewatch_upscale_queue gauge",
        f"facewatch_upscale_queue {m['upscale_queue']}",
        "# HELP facewatch_camera_fps Фактический FPS детекции по камере",
        "# TYPE facewatch_camera_fps gauge",
    ]
    for cam_id, value in m["camera_fps"].items():
        lines.append(f'facewatch_camera_fps{{camera="{cam_id}"}} {value}')
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


@router.get("/record-layer")
async def record_layer_status(_=Depends(require_role("admin", "operator")),
                              db: AsyncSession = Depends(get_db)):
    """Состояние слоя записи: статус каждого потока (SPEC §14, §9).

    Данные собирает воркер и кладёт в Redis (`record:layer`). Бэкенд к
    Control API MediaMTX не ходит намеренно: доступ к нему равен доступу к
    RTSP-адресам всех камер с учётными данными, и наружу он не
    публикуется (решение цикла 24). Ключ живёт 120 секунд — при
    остановленном воркере интерфейс покажет «данных нет», а не молча
    застывшую картину недельной давности.

    Сегменты за сутки считаются здесь, а не в воркере: это запрос к
    архиву, и держать его в цикле менеджера (раз в 10 секунд на 120
    камерах) незачем — интерфейс обновляется куда реже.
    """
    payload = None
    try:
        raw = await get_redis().get("record:layer")
        if raw:
            payload = json.loads(raw)
    except Exception:
        payload = None

    day_ago = datetime.utcnow() - timedelta(days=1)
    segments_day = (await db.execute(
        select(func.count(VideoSegment.id)).where(VideoSegment.started_at >= day_ago)
    )).scalar() or 0
    bytes_day = int((await db.execute(
        select(func.coalesce(func.sum(VideoSegment.size_bytes), 0))
        .where(VideoSegment.started_at >= day_ago)
    )).scalar() or 0)
    enabled = (await db.execute(
        select(func.count(Camera.id)).where(Camera.enabled.is_(True))
    )).scalar() or 0

    if payload is None:
        # Воркер молчит: честное «нет данных» вместо нулей, которые
        # выглядели бы как «все 120 потоков потеряны».
        return {
            "available": False,
            "reason": "воркер не публиковал состояние слоя записи",
            "cameras_enabled": enabled,
            "segments_last_day": segments_day,
            "gb_last_day": round(bytes_day / BYTES_PER_GB, 2),
            "streams": [], "summary": None, "segment_gaps": [],
            "analytics": None,
        }

    return {
        "available": True,
        "updated_at": payload.get("updated_at"),
        "cameras_enabled": enabled,
        "segments_last_day": segments_day,
        "gb_last_day": round(bytes_day / BYTES_PER_GB, 2),
        "streams": payload.get("streams") or [],
        "summary": payload.get("summary"),
        "segment_gaps": payload.get("segment_gaps") or [],
        # Состояние слоя аналитики: отказ загрузки модели больше не роняет
        # воркер (SPEC §2), поэтому его надо где-то показать — иначе
        # «распознавание молчит» неотличимо от «в кадре никого нет».
        "analytics": payload.get("analytics"),
    }


async def _setting_int(db: AsyncSession, key: str, default: int) -> int:
    row = await db.get(Setting, key)
    try:
        return int(row.value) if row else default
    except (TypeError, ValueError):
        return default


@router.get("/storage")
async def storage_report(_=Depends(require_role("admin", "operator")),
                         db: AsyncSession = Depends(get_db)):
    """Состояние хранилища архива: заполнение, расход, прогноз (SPEC §5, §21).

    Прогноз считается по **фактическому** расходу за последние сутки, а не
    по номиналу `Mbps × 10.8`: SPEC §21 сам называет номинал завышенным
    («с учётом VBR/smart-кодек фактически ~1.6–2 ТБ/сутки» против 2.6 ТБ
    номинала), и прогноз по нему занижал бы срок хранения примерно в
    полтора раза. Номинал остаётся в ответе рядом — как раз для калибровки,
    которую требует §21.

    Пока фактических данных нет (первые сутки после развёртывания, все
    сегменты со `size_bytes` = 0 от старых строк), прогноз падает обратно
    на номинал по числу включённых камер: показать прочерк администратору,
    который только что развернул систему и хочет знать, хватит ли диска, —
    хуже, чем показать расчётную оценку с явной пометкой источника.
    """
    try:
        du = shutil.disk_usage(settings.MEDIA_PATH)
        total, used, free = du.total, du.used, du.free
    except OSError:
        total = used = free = 0

    day_ago = datetime.utcnow() - timedelta(days=1)
    cams = (await db.execute(select(Camera))).scalars().all()
    enabled = [c for c in cams if c.enabled]

    # int() обязателен: SUM() по bigint Postgres возвращает numeric, драйвер
    # отдаёт его как decimal.Decimal, и любое деление на float (прогноз,
    # перевод в ГБ) падает с TypeError. Отдельная строка, а не приведение по
    # месту, — деление тут ниже в четырёх местах.
    measured_bytes = int((await db.execute(
        select(func.coalesce(func.sum(VideoSegment.size_bytes), 0))
        .where(VideoSegment.started_at >= day_ago)
    )).scalar() or 0)
    archive_bytes = int((await db.execute(
        select(func.coalesce(func.sum(VideoSegment.size_bytes), 0))
    )).scalar() or 0)
    segments_day = (await db.execute(
        select(func.count(VideoSegment.id)).where(VideoSegment.started_at >= day_ago)
    )).scalar() or 0

    # Номинал считается по фактическому битрейту основного потока из SPEC §1
    # (2048 kbps). Это не настройка: слой записи ведёт remux потока как есть
    # (§24 запрещает перекодирование), поэтому битрейт задаёт камера, а не
    # система, и «настроить» его здесь было бы враньём.
    nominal_per_day_gb = nominal_gb_per_day(MAIN_STREAM_KBPS, len(enabled))
    measured_per_day_gb = measured_bytes / BYTES_PER_GB

    source = "measured" if measured_bytes > 0 else "nominal"
    per_day_bytes = measured_bytes if measured_bytes > 0 else nominal_per_day_gb * BYTES_PER_GB
    left = days_left(free, per_day_bytes)

    warn = await _setting_int(db, "disk_warn_pct", int(DISK_WARN_PCT))
    crit = await _setting_int(db, "disk_crit_pct", int(DISK_CRIT_PCT))
    used_pct = round(used * 100.0 / total, 1) if total else 0.0
    global_retention = await _setting_int(db, "retention_days", 14)

    return {
        "disk_total_gb": round(total / BYTES_PER_GB, 1),
        "disk_used_gb": round(used / BYTES_PER_GB, 1),
        "disk_free_gb": round(free / BYTES_PER_GB, 1),
        "disk_used_percent": used_pct,
        "alert_level": disk_alert_level(used_pct, warn, crit),
        "warn_percent": warn,
        "crit_percent": crit,
        "archive_gb": round(archive_bytes / BYTES_PER_GB, 1),
        "segments_last_day": segments_day,
        "measured_gb_per_day": round(measured_per_day_gb, 2),
        "nominal_gb_per_day": round(nominal_per_day_gb, 2),
        # Коэффициент калибровки (§21): >1 — расходуется быстрее расчёта.
        "calibration": (lambda k: round(k, 3) if k is not None else None)(
            calibration(measured_per_day_gb, nominal_per_day_gb)
        ),
        "forecast_source": source,
        "days_left": round(left, 1) if left is not None else None,
        "cameras_recording": len(enabled),
        "retention_days": global_retention,
        # Камеры с собственной глубиной хранения — чтобы администратор
        # видел отклонения от глобальной настройки, не открывая каждую.
        "per_camera_retention": {
            str(c.id): c.retention_days for c in cams if c.retention_days
        },
    }


@router.get("/storage/calculator")
async def storage_calculator(
    bitrate_kbps: int = Query(2048, ge=64, le=100_000),
    cameras: int = Query(120, ge=1, le=1000),
    days: int = Query(14, ge=1, le=3650),
    _=Depends(require_role("admin")),
):
    """Калькулятор хранения SPEC §21: битрейт × камеры × дни → требуемый объём.

    Отдельно от `/storage`: там — что происходит сейчас, здесь — «что если»,
    и параметры приходят от администратора, а не из БД. Границы у всех трёх
    numeric-параметров заданы явно (`Query(ge=, le=)`), иначе
    `days=999999999` даёт переполнение в интерфейсе на пустом месте.
    """
    gb = required_gb(bitrate_kbps, cameras, days)
    return {
        "bitrate_kbps": bitrate_kbps,
        "cameras": cameras,
        "days": days,
        "gb_per_day_per_camera": round(nominal_gb_per_day(bitrate_kbps), 2),
        "gb_per_day_total": round(nominal_gb_per_day(bitrate_kbps, cameras), 1),
        "required_gb": round(gb, 1),
        "required_tb": round(gb / 1024, 2),
    }
