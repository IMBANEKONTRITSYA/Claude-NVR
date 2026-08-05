"""Системный мониторинг (ТЗ 12): метрики хоста, состояние сервисов, Prometheus."""
import shutil
from datetime import datetime

import psutil
from fastapi import APIRouter, Depends, Response
from sqlalchemy import select, func

from ..config import settings
from ..db import SessionLocal
from ..models import Camera, FaceEvent, VideoSegment
from ..auth import require_role, require_role_query
from ..services.pubsub import get_redis

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
