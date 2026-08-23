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
from ..services.sensors import cpu_temperature
from ..services.system_metrics_acl import filter_system_metrics
from ..profiles import profile_settings
from ..services import autoconfig
from ..services.pubsub import get_redis
from ..services.storage import (BYTES_PER_GB, DISK_CRIT_PCT, DISK_WARN_PCT,
                                calibration, days_left, disk_alert_level,
                                nominal_gb_per_day, required_gb)
from .cameras import DEFAULT_ANALYTICS_MAX

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

    # Температура доступна не на всех платформах (в Docker под Windows — нет).
    #
    # Выбор датчика — не «первый попавшийся»: на целевом сервере §20
    # (2× Xeon, RAID-массив) в словаре лежат и корпусный `acpitz`, и
    # `drivetemp` каждого диска, а порядок обхода задаётся загрузкой
    # модулей ядра. Правило вынесено в services/sensors.py, потому что
    # проверять его надо без нужного железа — здесь его нет ни в
    # песочнице, ни на раннере CI.
    temp = temp_source = None
    try:
        reading = cpu_temperature(psutil.sensors_temperatures() or {})
        if reading is not None:
            temp, temp_source = reading.celsius, reading.source
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
    analytics_source: dict[str, dict] = {}
    try:
        r = get_redis()
        await r.ping()
        queue_len = await r.llen("upscale:queue")
        # Воркер публикует свой FPS по каждой камере в хеш worker:fps
        raw = await r.hgetall("worker:fps")
        fps = {k: float(v) for k, v in (raw or {}).items()}
        # SPEC §2/§15: источником кадров аналитики допустим основной поток
        # либо субпоток «с разрешением не ниже 640×480». Какой из них
        # реально достался камере — решает воркер по измеренному кадру
        # (worker/analytics_source.py), и без этой строки §9 показывал бы
        # FPS детекции, не говоря, по какому потоку он получен.
        raw_src = await r.hgetall("worker:analytics_source")
        for k, v in (raw_src or {}).items():
            try:
                row = json.loads(v)
            except (ValueError, TypeError):
                # Мусор в хеше не должен ронять весь /metrics: камера
                # просто останется без строки, остальные метрики уедут.
                continue
            # Валидный JSON — ещё не объект: строка «"5"» разбирается в
            # число, и фронтенд полез бы за полями в него. Отбрасывается
            # тем же путём, что и мусор.
            if isinstance(row, dict):
                analytics_source[k] = row
    except Exception:
        redis_ok = False

    return {
        "cpu_percent": cpu,
        "ram_percent": mem.percent,
        "ram_used_mb": round(mem.used / 1048576),
        "ram_total_mb": round(mem.total / 1048576),
        "temperature_c": temp,
        # Источник показания едет рядом с числом: 38 °C с сокета и
        # 38 °C с корпусного датчика — разные сведения об объекте,
        # а подпись «Температура» без уточнения выдаёт второе за
        # первое (см. services/sensors.py).
        "temperature_source": temp_source,
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
        "camera_analytics_source": analytics_source,
    }


@router.get("/metrics")
async def system_metrics(user=Depends(require_role("admin", "operator"))):
    """Метрики для админ-дашборда.

    Оператору — ограниченно, как требует §18: телеметрия железа сервера
    (CPU, RAM, температура) не отдаётся, место на диске отдаётся. Граница
    и её обоснование — в services/system_metrics_acl.py; до цикла 55 эта
    строка докстринга была единственным местом, где ограничение
    существовало, — оператор получал ответ целиком.
    """
    return filter_system_metrics(await _collect(), user.role)


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
            "analytics": None, "control_api_error": None,
            "record_root_warning": None,
        }

    # Состояние восстановления (SPEC §19) приезжает от воркера отдельной
    # картой по camera_id и здесь приклеивается к своему потоку: интерфейс
    # показывает его в строке камеры, и раскладывать в нём вторую карту по
    # ключу означало бы держать в UI то же соединение, только руками.
    # Ключи после JSON — строки, поэтому сравнение идёт по str(camera_id).
    recovery = payload.get("recovery") or {}
    streams = payload.get("streams") or []
    if isinstance(recovery, dict):
        for stream in streams:
            info = recovery.get(str(stream.get("camera_id")))
            if info:
                stream["recovery"] = info

    return {
        "available": True,
        "updated_at": payload.get("updated_at"),
        "cameras_enabled": enabled,
        "segments_last_day": segments_day,
        "gb_last_day": round(bytes_day / BYTES_PER_GB, 2),
        "streams": streams,
        "summary": payload.get("summary"),
        "segment_gaps": payload.get("segment_gaps") or [],
        # Состояние слоя аналитики: отказ загрузки модели больше не роняет
        # воркер (SPEC §2), поэтому его надо где-то показать — иначе
        # «распознавание молчит» неотличимо от «в кадре никого нет».
        "analytics": payload.get("analytics"),
        "control_api_error": payload.get("control_api_error"),
        # Корень, куда MediaMTX пишет сегменты, разведён с тем, который
        # сканирует архив (SPEC §5 «путь архива конфигурируется»). Симптом
        # такого расхождения — «пропуск записи» разом на всех камерах и
        # диск, который никто не чистит; сам по себе он на причину не
        # указывает, поэтому причина едет отдельным полем.
        "record_root_warning": payload.get("record_root_warning"),
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
    cameras: int | None = Query(None, ge=1, le=1000),
    days: int | None = Query(None, ge=1, le=3650),
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Калькулятор хранения SPEC §16: битрейт × камеры × дни → требуемый объём.

    Отдельно от `/storage`: там — что происходит сейчас, здесь — «что если».

    **Умолчания берутся из системы, а не из константы.** Раньше здесь стояло
    `cameras = 120` и `days = 14`, и это было нарушением §22 («Запрещено
    хардкодить количество камер») с прямым следствием для пользователя: у
    администратора объекта на 32 камеры калькулятор при каждом открытии
    считал объём для 120 — то есть выдавал заведомо неверный ответ до того,
    как человек что-то введёт, а §16 требует ровно обратного, «формулы и
    калькуляторы вместо фиксированных чисел».

    Теперь `cameras` по умолчанию — фактическое число включённых камер, а
    `days` — глобальная настройка retention. Оба параметра остаются
    переопределяемыми: смысл калькулятора «что если» никуда не делся, и
    сценарий «а если камер станет вдвое больше» работает как работал.

    Что именно подставилось, видно в ответе (`cameras_source`,
    `days_source`): интерфейс обязан отличать «посчитано для вашей системы»
    от «посчитано для введённого вами числа», иначе подстановка становится
    новым молчаливым умолчанием — тем же, от которого уходим.

    Границы у всех трёх numeric-параметров заданы явно (`Query(ge=, le=)`),
    иначе `days=999999999` даёт переполнение в интерфейсе на пустом месте.
    """
    cameras_source = "requested"
    if cameras is None:
        cameras_source = "actual"
        enabled = (await db.execute(
            select(func.count()).select_from(Camera).where(Camera.enabled == True)  # noqa: E712
        )).scalar_one()
        # Пустая система (камер ещё не завели) — считаем для одной: ноль
        # обнулил бы весь расчёт и показал «нужно 0 ГБ», что выглядит как
        # ответ, хотя это отсутствие данных.
        cameras = max(int(enabled), 1)
        if not enabled:
            cameras_source = "fallback_empty"

    days_source = "requested"
    if days is None:
        days_source = "actual"
        days = await _setting_int(db, "retention_days", 14)

    gb = required_gb(bitrate_kbps, cameras, days)
    return {
        "bitrate_kbps": bitrate_kbps,
        "cameras": cameras,
        "cameras_source": cameras_source,
        "days": days,
        "days_source": days_source,
        "gb_per_day_per_camera": round(nominal_gb_per_day(bitrate_kbps), 2),
        "gb_per_day_total": round(nominal_gb_per_day(bitrate_kbps, cameras), 1),
        "required_gb": round(gb, 1),
        "required_tb": round(gb / 1024, 2),
    }


# --- Автоконфигурация при первом запуске (SPEC §16) ---------------------

# Отметка о применённой автоконфигурации. Ключ живёт в той же таблице
# settings, но сознательно не входит в SCHEMA роутера настроек: это не
# параметр, который администратор правит формой, а след действия. Через
# PUT /api/settings он неизменяем именно поэтому.
AUTOCONFIG_APPLIED_KEY = "autoconfig_applied_at"


async def _autoconfig_context(db: AsyncSession, bitrate_kbps: int,
                              retention_days: int | None) -> dict:
    """Ресурсы хоста + предложение + то, что настроено сейчас (SPEC §16)."""
    res = autoconfig.detect_resources(settings.MEDIA_PATH)
    days = retention_days or await _setting_int(db, "retention_days", 14)

    # Ядра берутся физические, а не логические. Вилки §16 («0.02-0.04 ядра
    # на камеру») сняты для ядер; принять за ядро поток HT/SMT значит
    # удвоить предложение на ровном месте — на целевом сервере §20 (2× Xeon)
    # это 64 потока против 32 ядер, то есть вдвое больше обещанных камер
    # аналитики, чем сервер вывезет.
    plan = autoconfig.plan(
        res["cores_physical"], res["ram_mb"], res["disk_free_gb"],
        bitrate_kbps=bitrate_kbps, retention_days=days, gpu=res["gpu"],
    )

    cams = (await db.execute(select(Camera))).scalars().all()
    applied = await db.get(Setting, AUTOCONFIG_APPLIED_KEY)
    profile_row = await db.get(Setting, "performance_profile")

    return {
        "resources": res,
        "plan": plan,
        "current": {
            "cameras_total": len(cams),
            "cameras_enabled": sum(1 for c in cams if c.enabled),
            "cameras_analytics": sum(1 for c in cams if (c.mode or "record_only") == "analytics"),
            "analytics_cameras_max": await _setting_int(
                db, "analytics_cameras_max", DEFAULT_ANALYTICS_MAX),
            "performance_profile": profile_row.value if profile_row else None,
        },
        # «Первый запуск» — это не только «система только что развёрнута»:
        # смысл в том, что предложение ещё ни разу не применяли и ни одной
        # камеры не завели. Ровно в этом состоянии экран стоит показывать
        # самому, а не ждать, пока администратор его найдёт.
        "first_run": applied is None and not cams,
        "applied_at": applied.value if applied else None,
    }


@router.get("/autoconfig")
async def autoconfig_report(
    bitrate_kbps: int = Query(MAIN_STREAM_KBPS, ge=64, le=100_000),
    retention_days: int | None = Query(None, ge=1, le=3650),
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Что сервер потянет и что предлагается настроить (SPEC §16).

    Только считает и показывает — ничего не меняет. Применение вынесено в
    отдельный POST ниже: §16 говорит «предлагает», и молча переписанные
    настройки на первом же открытии экрана мониторинга были бы совсем
    другим поведением, чем предложение.

    `bitrate_kbps` и `retention_days` — параметры «что если»: предел по
    диску целиком определяется ими, и администратор объекта, который знает
    свои камеры лучше константы §1, должен иметь возможность подставить
    свои числа, не трогая настройки системы.
    """
    return await _autoconfig_context(db, bitrate_kbps, retention_days)


@router.post("/autoconfig/apply")
async def autoconfig_apply(
    bitrate_kbps: int = Query(MAIN_STREAM_KBPS, ge=64, le=100_000),
    retention_days: int | None = Query(None, ge=1, le=3650),
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Применяет предложение §16: предел камер аналитики и профиль.

    Записываются ровно две вещи, и обе — про слой аналитики: предел
    `analytics_cameras_max` и профиль производительности со всеми его
    параметрами. Число камер записи не настройка, а следствие того,
    сколько камер завели, — его автоконфигурация показывает, но применять
    ей нечего.

    Retention не трогается сознательно, хотя предел по диску считается
    именно от него: уменьшить глубину архива — это выбросить записи, и
    такое решение не принимают за администратора кнопкой «применить
    рекомендацию».
    """
    ctx = await _autoconfig_context(db, bitrate_kbps, retention_days)
    plan = ctx["plan"]

    # Схема настройки не допускает нуля (нижняя граница 1), а рекомендация
    # его допускает — это разные вопросы: «сколько камер потянет сервер» и
    # «какой потолок выставить». При нуле пишется 1 и возвращается
    # предупреждение из плана: тихо записать единицу и промолчать значило
    # бы выдать «одна камера аналитики допустима» за рекомендацию системы.
    analytics_max = max(autoconfig.ANALYTICS_SETTING_MIN, plan["analytics_max"])

    values = profile_settings(plan["profile"])
    values["performance_profile"] = plan["profile"]
    values["analytics_cameras_max"] = str(analytics_max)
    values[AUTOCONFIG_APPLIED_KEY] = datetime.utcnow().isoformat(timespec="seconds") + "Z"

    for key, val in values.items():
        existing = await db.get(Setting, key)
        if existing:
            existing.value = val
        else:
            db.add(Setting(key=key, value=val))
    await db.commit()

    return {
        "applied": {
            "analytics_cameras_max": analytics_max,
            "performance_profile": plan["profile"],
        },
        "recommended_analytics_max": plan["analytics_max"],
        "recording_max": plan["recording_max"],
        "warnings": plan["warnings"],
        "applied_at": values[AUTOCONFIG_APPLIED_KEY],
    }
