import os
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Query, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, func
from ..config import settings
from ..db import get_db
from ..models import Camera, Setting
from ..auth import require_role, require_role_query, get_current_user, get_user_from_query_token
from ..schemas import (OnvifBulkAddRequest, OnvifDescribeRequest, CameraIn, CameraOut,
                       OnvifProfilesRequest, OnvifStreamUriRequest, PtzMoveIn,
                       PtzPresetGotoIn, PtzPresetSaveIn, ROIIn, RtspTest)
from ..services import camera_config, csv_export
from ..services.encryption import encrypt, decrypt
from ..services.pubsub import get_redis

router = APIRouter(prefix="/api/cameras", tags=["cameras"])

# Потолки импорта (SPEC §3, §16: масштаб объекта — 12–250+ камер).
# 1000 строк с запасом перекрывают верх диапазона; ограничение нужно не
# ради него, а чтобы разбор не стал способом занять память бэкенда
# файлом на сотни мегабайт. 4 МБ — тот же запас по размеру: строка
# конфигурации камеры это ~200 байт.
MAX_IMPORT_ROWS = 1000
MAX_IMPORT_BYTES = 4 * 1024 * 1024

# Сколько камер разрешено держать в режиме analytics, если настройка не
# задана. SPEC §1 числа по умолчанию не задаёт («от 0 до N,
# конфигурируется, зависит от возможностей сервера»), поэтому двойка —
# это осторожный старт, а не требование ТЗ: на первом запуске о ресурсах
# сервера ещё ничего не известно, а предложение автоконфигурации (§16)
# администратор увидит и применит отдельным действием.
DEFAULT_ANALYTICS_MAX = 2


def _camera_out(c: Camera) -> CameraOut:
    return CameraOut(
        id=c.id, name=c.name, location=c.location, enabled=c.enabled,
        mode=c.mode or "record_only", status=c.status,
        has_substream=bool(c.sub_rtsp_url_enc), motion_sensitivity=c.motion_sensitivity,
        onvif_enabled=bool(c.onvif_enabled), has_onvif=bool(c.onvif_host),
        retention_days=c.retention_days,
        onvif_host=c.onvif_host, onvif_port=c.onvif_port,
        onvif_username=c.onvif_username,
        detection_schedule=c.detection_schedule,
        record_on_motion=bool(c.record_on_motion),
    )


def _check_motion_recording(payload) -> None:
    """SPEC §6: «запись только при движении» требует режима analytics.

    Источник движения есть только у камеры, которую кто-то декодирует:
    MOG2-префильтр и события ONVIF читает нить аналитики, а `record_only`
    камеру не открывает никто (§2 — слой записи идёт remux'ом, без
    декодирования). Флаг на такой камере не ошибочен по данным, он просто
    никогда ничего не сделает: наблюдения не будет, покрытия не будет, ни
    один сегмент не удалится.

    Молча принять его хуже, чем отказать: администратор считает, что диск
    экономится, и обнаружит обратное по заполнению тома. Поэтому отказ с
    объяснением, а не тихое сохранение мёртвой настройки.
    """
    if payload.record_on_motion and payload.mode != "analytics":
        raise HTTPException(
            400,
            "«Запись только при движении» доступна камерам в режиме "
            "«аналитика»: движение определяет слой аналитики, а камера "
            "в режиме «только запись» не декодируется вовсе.",
        )


async def _analytics_limit(db: AsyncSession) -> int:
    row = await db.get(Setting, "analytics_cameras_max")
    try:
        return max(1, int(row.value)) if row else DEFAULT_ANALYTICS_MAX
    except (TypeError, ValueError):
        return DEFAULT_ANALYTICS_MAX


async def _ensure_analytics_slot(db: AsyncSession, exclude_id: int | None = None) -> None:
    """Не даёт перевести в analytics больше камер, чем разрешено.

    SPEC §1 делает число камер аналитики настройкой («от 0 до N,
    конфигурируется, зависит от возможностей сервера»), а §16 задаёт её
    цену — 0.5-1.5 ядра на камеру. Без явного предела
    ничто не мешает администратору включить аналитику на всех камерах —
    слой записи при этом устоит (он независим, §2), а вот воркер уйдёт в
    неограниченное отставание, и деградация будет выглядеть как «система
    тормозит», а не как «включено больше, чем рассчитано».
    """
    limit = await _analytics_limit(db)
    q = select(func.count()).select_from(Camera).where(Camera.mode == "analytics")
    if exclude_id is not None:
        q = q.where(Camera.id != exclude_id)
    current = (await db.execute(q)).scalar() or 0
    if current >= limit:
        raise HTTPException(
            400,
            f"Камер в режиме аналитики уже {current} — это предел "
            f"(настройка analytics_cameras_max = {limit}). Переведите другую "
            "камеру в режим «только запись» или увеличьте предел в настройках.",
        )


@router.get("", response_model=list[CameraOut])
async def list_cameras(_=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Camera).order_by(Camera.id))
    return [_camera_out(c) for c in r.scalars().all()]


@router.post("", response_model=CameraOut)
async def add_camera(payload: CameraIn, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    _check_motion_recording(payload)
    if payload.mode == "analytics":
        await _ensure_analytics_slot(db)
    cam = Camera(
        name=payload.name,
        rtsp_url_enc=encrypt(payload.rtsp_url),
        sub_rtsp_url_enc=encrypt(payload.sub_rtsp_url) if payload.sub_rtsp_url else None,
        location=payload.location,
        enabled=payload.enabled,
        mode=payload.mode,
        motion_sensitivity=payload.motion_sensitivity,
        status="offline",
        onvif_enabled=payload.onvif_enabled,
        onvif_host=payload.onvif_host or None,
        onvif_port=payload.onvif_port,
        onvif_username=payload.onvif_username or None,
        onvif_password_enc=encrypt(payload.onvif_password) if payload.onvif_password else None,
        retention_days=payload.retention_days,
        # SPEC §6. None остаётся None («круглосуточно»), а не пустым
        # расписанием: у колонки эти два значения означают одно и то же,
        # но NULL честнее показывает, что расписание не задавали.
        detection_schedule=(payload.detection_schedule.model_dump()
                            if payload.detection_schedule else None),
        record_on_motion=payload.record_on_motion,
    )
    db.add(cam)
    await db.commit()
    await db.refresh(cam)
    await get_redis().publish("cameras:changed", str(cam.id))
    return _camera_out(cam)


@router.put("/{cam_id}", response_model=CameraOut)
async def update_camera(cam_id: int, payload: CameraIn, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    _check_motion_recording(payload)
    cam.name = payload.name
    cam.rtsp_url_enc = encrypt(payload.rtsp_url)
    # Пустое поле субпотока очищает его, отсутствующее — оставляет прежнее значение
    if payload.sub_rtsp_url is not None:
        cam.sub_rtsp_url_enc = encrypt(payload.sub_rtsp_url) if payload.sub_rtsp_url else None
    if payload.mode == "analytics" and cam.mode != "analytics":
        await _ensure_analytics_slot(db, exclude_id=cam.id)
    cam.location = payload.location
    cam.enabled = payload.enabled
    cam.mode = payload.mode
    cam.motion_sensitivity = payload.motion_sensitivity
    cam.onvif_enabled = payload.onvif_enabled
    cam.onvif_host = payload.onvif_host or None
    cam.onvif_port = payload.onvif_port
    cam.onvif_username = payload.onvif_username or None
    # Пароль, как и sub_rtsp_url: пустое значение из формы не должно
    # затирать уже сохранённый пароль при обычном редактировании других полей.
    if payload.onvif_password:
        cam.onvif_password_enc = encrypt(payload.onvif_password)
    # В отличие от пароля и субпотока, пустое значение здесь значимо: это
    # «убрать собственный срок, следовать за глобальным». Отличить его от
    # «поле не прислано» на модели с дефолтом None нельзя, и трактовка
    # выбрана в пользу той, что выражается формой, — иначе снять
    # собственный срок было бы нечем.
    cam.retention_days = payload.retention_days
    # То же правило, что и у retention_days: пустое значение значимо —
    # «снять расписание, вернуть круглосуточную детекцию».
    cam.detection_schedule = (payload.detection_schedule.model_dump()
                              if payload.detection_schedule else None)
    cam.record_on_motion = payload.record_on_motion
    await db.commit()
    await db.refresh(cam)
    await get_redis().publish("cameras:changed", str(cam.id))
    return _camera_out(cam)


@router.delete("/{cam_id}")
async def delete_camera(cam_id: int, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    await db.execute(delete(Camera).where(Camera.id == cam_id))
    await db.commit()
    await get_redis().publish("cameras:changed", str(cam_id))
    return {"ok": True}


@router.patch("/{cam_id}/enabled")
async def toggle_enabled(cam_id: int, enabled: bool, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    cam.enabled = enabled
    if not enabled:
        cam.status = "disabled"
    await db.commit()
    await db.refresh(cam)
    await get_redis().publish("cameras:changed", str(cam.id))
    return {"id": cam.id, "enabled": cam.enabled, "status": cam.status}


@router.get("/{cam_id}/rtsp")
async def get_rtsp(cam_id: int, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    return {"rtsp_url": decrypt(cam.rtsp_url_enc)}


@router.get("/{cam_id}/roi")
async def get_roi(cam_id: int, _=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    return cam.roi or {"polygons": []}


@router.put("/{cam_id}/roi")
async def put_roi(cam_id: int, payload: ROIIn, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    # SPEC §11: «Scale Edition: доступно только для камер в режиме
    # analytics». Зоны детекции у камеры, которая только пишется, ни на что
    # не влияют — сохранить их значит показать оператору настройку, которая
    # молча ничего не делает.
    if (cam.mode or "record_only") != "analytics":
        raise HTTPException(400, "Зоны детекции задаются только для камер в режиме аналитики")
    cam.roi = payload.model_dump()
    await db.commit()
    return {"ok": True}


@router.get("/{cam_id}/snapshot")
async def snapshot(cam_id: int, _=Depends(get_user_from_query_token)):
    """Последний кадр камеры (Стена, дашборд) — токен в query, потому что
    <img src> не умеет слать заголовок Authorization.

    Роль не проверяется намеренно: матрица прав ТЗ разрешает живой просмотр
    всем трём ролям, тот же контракт, что у hls_auth(). Но существование
    учётной записи проверяется по БД, а не одной лишь подписью токена, —
    иначе удалённый пользователь продолжал бы смотреть кадры со всех камер
    до истечения access-токена.
    """
    path = os.path.join(settings.MEDIA_PATH, "snapshots", f"cam{cam_id}_latest.jpg")
    if not os.path.exists(path):
        raise HTTPException(404, "Нет кадра")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{cam_id}/hls")
async def hls_url(cam_id: int, _=Depends(get_current_user)):
    """URL HLS-плейлиста MediaMTX, прокидываемого через nginx."""
    return {"url": f"/hls/cam{cam_id}/index.m3u8"}


@router.get("/hls-auth", include_in_schema=False)
async def hls_auth(_=Depends(get_current_user)):
    """Внутренний эндпоинт для nginx `auth_request` (location /hls/ в
    nginx-locations.conf). До этого фикса /hls/ проксировался в MediaMTX без
    какой-либо проверки — любой, у кого есть сетевой доступ к nginx, мог
    смотреть видео с любой камеры вообще без логина, просто перебирая
    cam{id} (P0, цикл 6). Роль не проверяется намеренно: матрица прав ТЗ
    разрешает просмотр видео онлайн всем трём ролям — здесь важна только
    валидность токена, тот же контракт, что у snapshot()/archive/prometheus."""
    return {"ok": True}


@router.get("/onvif/discover")
async def onvif_discover(
    subnet: str | None = Query(None, description="CIDR для перебора, например 192.168.1.0/24"),
    _=Depends(require_role("admin")),
):
    """Автообнаружение ONVIF-камер в сети (ТЗ 18.7: "автообнаружение камер
    в сети"). Выполняется воркером (worker/onvif_api.py) — он в той же
    docker-сети, что и камеры, backend туда прямого сетевого пути не имеет.
    admin-only, как и остальное управление камерами.

    Необязательный subnet включает перебор адресов диапазона вместо одной
    лишь multicast-рассылки WS-Discovery: multicast не проходит через NAT
    docker-сети (на Docker Desktop под Windows — гарантированно), из-за чего
    поиск не находил камер вообще. Таймаут запроса к воркеру при переборе
    выше: 1024 адреса × несколько портов не укладываются в 15 секунд.
    """
    timeout = 180 if subnet else 15
    params = {"subnet": subnet} if subnet else None
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.get(f"{settings.WORKER_URL}/onvif/discover", params=params)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Не удалось выполнить автообнаружение")
    return {"devices": data.get("devices", []), "warnings": data.get("warnings", [])}


@router.post("/onvif/profiles")
async def onvif_profiles(payload: OnvifProfilesRequest, _=Depends(require_role("admin"))):
    """Получение профилей потоков ONVIF-камеры (ТЗ 18.7, вторая часть:
    "получение профилей потоков") — GetProfiles Media-сервиса. Учётные
    данные приходят в теле POST от формы камеры (ещё не обязательно
    сохранённой), не персистятся здесь; проксируется воркеру по той же
    причине, что и /onvif/discover — backend не имеет прямого сетевого
    пути к камерам, воркер в той же docker-сети."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(f"{settings.WORKER_URL}/onvif/profiles", json=payload.model_dump())
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Не удалось получить профили потоков")
    return {"profiles": data.get("profiles", [])}


@router.post("/onvif/stream-uri")
async def onvif_stream_uri(payload: OnvifStreamUriRequest, _=Depends(require_role("admin"))):
    """RTSP-адрес потока для выбранного ONVIF-профиля (GetStreamUri) —
    завершает ТЗ 18.7: результат подставляется фронтендом в поле rtsp_url
    формы камеры вместо ручного ввода."""
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            resp = await client.post(f"{settings.WORKER_URL}/onvif/stream-uri", json=payload.model_dump())
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Не удалось получить адрес потока")
    return {"uri": data.get("uri")}


# --- PTZ (SPEC §4: «PTZ-управление (если поддерживается камерой)») ---------
#
# Роли: admin и operator, наблюдателю запрещено. В матрице §18 отдельной
# строки для PTZ нет, а из имеющихся ближе всего «Настройка ROI» (Да / Да /
# Нет) — такое же действие над камерой из живого просмотра. Определяющее
# соображение: наблюдателю матрица оставляет только чтение, а поворот
# камеры меняет то, что видят все остальные, и уводит обзор с точки —
# «Просмотр видео онлайн» (Да / Да / Да) это не покрывает.
#
# Учётные данные берутся из БД (расшифровываются), а не приходят от клиента,
# как в /onvif/profiles: там камера ещё не сохранена и пароль вводится в
# форме, здесь камера уже заведена. Оператор при этом не получает доступа к
# самому паролю — только к командам поворота.


async def _ptz_camera(cam_id: int, db: AsyncSession) -> Camera:
    """Камера с настроенным ONVIF — иначе PTZ невозможен физически."""
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    if not cam.onvif_host:
        raise HTTPException(400, "У камеры не настроен ONVIF — PTZ недоступен")
    return cam


def _ptz_payload(cam: Camera, extra: dict | None = None) -> dict:
    payload = {
        "host": cam.onvif_host,
        "port": cam.onvif_port or 80,
        "username": cam.onvif_username or None,
        "password": decrypt(cam.onvif_password_enc) if cam.onvif_password_enc else None,
    }
    payload.update(extra or {})
    return payload


async def _ptz_call(path: str, payload: dict, *, timeout: float = 15) -> dict:
    """Запрос к воркеру: он в одной сети с камерами, backend — нет.

    Таймаут на команду поворота короткий (см. вызовы ниже): PTZ — интерактивное
    действие, оператор держит кнопку, и ответ, пришедший через 15 секунд,
    бесполезен — за это время он уже отпустил её и нажал снова.
    """
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(f"{settings.WORKER_URL}{path}", json=payload)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    return resp.json()


@router.get("/{cam_id}/ptz")
async def ptz_capabilities(cam_id: int, _=Depends(require_role("admin", "operator")),
                           db: AsyncSession = Depends(get_db)):
    """Поддерживает ли камера PTZ и какие у неё пресеты.

    Ответ `supported: false` — штатный для фиксированной камеры: интерфейс по
    нему просто не рисует пульт. Недоступность камеры тоже не 502: пульт не
    должен исчезать из-за того, что камера моргнула сетью, поэтому причина
    возвращается в `error` вместе с `supported: false`.
    """
    cam = await _ptz_camera(cam_id, db)
    data = await _ptz_call("/onvif/ptz/capabilities", _ptz_payload(cam))
    if not data.get("ok"):
        return {"supported": False, "presets": [], "error": data.get("error")}
    return {
        "supported": bool(data.get("supported")),
        "profile_token": data.get("profile_token"),
        "presets": data.get("presets", []),
    }


@router.post("/{cam_id}/ptz/move")
async def ptz_move(cam_id: int, payload: PtzMoveIn,
                   _=Depends(require_role("admin", "operator")),
                   db: AsyncSession = Depends(get_db)):
    """Непрерывный поворот/зум с заданной скоростью.

    Движение прекращается по /ptz/stop либо само — по таймауту, зашитому в
    ONVIF-команду (onvif_client.PTZ_MOVE_TIMEOUT_SEC). Второе важнее первого:
    если оператор закрыл вкладку, не отпустив кнопку, останавливать камеру
    будет некому.
    """
    cam = await _ptz_camera(cam_id, db)
    data = await _ptz_call("/onvif/ptz/move", _ptz_payload(cam, {
        "pan": payload.pan, "tilt": payload.tilt, "zoom": payload.zoom,
        "profile_token": payload.profile_token,
    }), timeout=8)
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Камера не приняла команду поворота")
    return {"ok": True, "profile_token": data.get("profile_token")}


@router.post("/{cam_id}/ptz/stop")
async def ptz_stop_move(cam_id: int, payload: PtzMoveIn,
                        _=Depends(require_role("admin", "operator")),
                        db: AsyncSession = Depends(get_db)):
    """Остановка поворота и зума."""
    cam = await _ptz_camera(cam_id, db)
    data = await _ptz_call("/onvif/ptz/stop", _ptz_payload(cam, {
        "profile_token": payload.profile_token,
    }), timeout=8)
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Камера не приняла команду остановки")
    return {"ok": True}


@router.post("/{cam_id}/ptz/preset/goto")
async def ptz_goto_preset(cam_id: int, payload: PtzPresetGotoIn,
                          _=Depends(require_role("admin", "operator")),
                          db: AsyncSession = Depends(get_db)):
    """Наведение камеры на сохранённую позицию."""
    cam = await _ptz_camera(cam_id, db)
    data = await _ptz_call("/onvif/ptz/preset/goto", _ptz_payload(cam, {
        "preset_token": payload.preset_token, "profile_token": payload.profile_token,
    }))
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Камера не приняла переход на позицию")
    return {"ok": True}


@router.post("/{cam_id}/ptz/preset/save")
async def ptz_save_preset(cam_id: int, payload: PtzPresetSaveIn,
                          _=Depends(require_role("admin", "operator")),
                          db: AsyncSession = Depends(get_db)):
    """Сохранение текущего положения камеры как именованной позиции."""
    cam = await _ptz_camera(cam_id, db)
    data = await _ptz_call("/onvif/ptz/preset/save", _ptz_payload(cam, {
        "name": payload.name, "profile_token": payload.profile_token,
    }))
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Камера не сохранила позицию")
    return {"ok": True, "preset_token": data.get("preset_token"),
            "presets": data.get("presets", [])}


@router.post("/test")
async def test_rtsp(payload: RtspTest, _=Depends(require_role("admin"))):
    """Проверка RTSP-подключения: открытие потока через ffprobe с таймаутом 10с."""
    import asyncio
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-rtsp_transport", "tcp",
            "-timeout", "5000000",
            "-show_entries", "stream=codec_type,codec_name,width,height",
            "-of", "default=nw=1", payload.rtsp_url,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "error": "Таймаут подключения (10 сек)"}
        if proc.returncode != 0:
            return {"ok": False, "error": (err.decode(errors="ignore").strip() or "Не удалось подключиться")[:300]}
        info = out.decode(errors="ignore").strip()
        return {"ok": True, "info": info}
    except FileNotFoundError:
        return {"ok": False, "error": "ffprobe недоступен на сервере"}


async def _onvif_describe(client: httpx.AsyncClient, item) -> dict:
    """Спрашивает у воркера имя и RTSP-адреса одной камеры."""
    resp = await client.post(
        f"{settings.WORKER_URL}/onvif/describe",
        json={
            "host": item.host, "port": item.port,
            "username": item.username, "password": item.password,
            "scopes": item.scopes,
        },
    )
    return resp.json()


@router.post("/onvif/describe")
async def onvif_describe(payload: OnvifDescribeRequest, _=Depends(require_role("admin"))):
    """Предлагаемое имя камеры и готовые RTSP-адреса основного и субпотока.

    Имя берётся из текстового OSD камеры, а если его нет — из ONVIF-скоупа,
    затем из модели устройства, затем из IP (см. suggest_camera_name в
    worker/onvif_client.py)."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            data = await _onvif_describe(client, payload)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    if not data.get("ok"):
        raise HTTPException(502, data.get("error") or "Не удалось опросить камеру")
    return data


@router.post("/onvif/bulk-add")
async def onvif_bulk_add(
    payload: OnvifBulkAddRequest,
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Добавляет разом все выбранные найденные камеры (ТЗ 18.7).

    Для каждой камеры запрашивается имя и оба потока, затем создаётся
    запись. Ошибка на одной камере не отменяет остальных: в сети из десятка
    устройств одно может оказаться недоступным или с другим паролем, и
    терять из-за него всю операцию — плохой обмен. Итог возвращается
    списком, чтобы интерфейс показал, что именно не получилось.

    Повторно уже заведённые камеры не создаются: сверка идёт по ONVIF-хосту,
    иначе второй запуск поиска задваивал бы весь список.
    """
    existing_hosts = {
        h for (h,) in (await db.execute(select(Camera.onvif_host).where(Camera.onvif_host.isnot(None)))).all()
    }

    import asyncio

    added, skipped, failed = [], [], []
    to_query = []
    for item in payload.cameras:
        if item.host in existing_hosts:
            skipped.append({"host": item.host, "reason": "камера с таким ONVIF-адресом уже добавлена"})
        else:
            to_query.append(item)

    # Опрос камер идёт параллельно: на каждую приходится пять SOAP-запросов
    # (сведения об устройстве, OSD, профили и два GetStreamUri), и в сети из
    # двух-трёх десятков камер последовательный обход занимал бы минуты.
    # Ограничение в 8 одновременных — чтобы не завалить воркер и сеть разом.
    semaphore = asyncio.Semaphore(8)

    async def _describe_one(client, item):
        async with semaphore:
            try:
                return item, await _onvif_describe(client, item), None
            except httpx.HTTPError as e:
                return item, None, f"сервис распознавания недоступен: {e}"

    async with httpx.AsyncClient(timeout=60) as client:
        results = await asyncio.gather(*(_describe_one(client, i) for i in to_query))

    for item, data, error in results:
        if error:
            failed.append({"host": item.host, "error": error})
            continue
        if not data.get("ok") or not data.get("rtsp_url"):
            failed.append({"host": item.host, "error": data.get("error") or "не удалось получить поток"})
            continue

        # Дубликат внутри самого запроса: одна и та же камера могла прийти
        # дважды, если её нашли и multicast'ом, и перебором подсети.
        if item.host in existing_hosts:
            skipped.append({"host": item.host, "reason": "камера с таким ONVIF-адресом уже добавлена"})
            continue

        cam = Camera(
            name=(item.name or "").strip() or data.get("name") or item.host,
            rtsp_url_enc=encrypt(data["rtsp_url"]),
            sub_rtsp_url_enc=encrypt(data["sub_rtsp_url"]) if data.get("sub_rtsp_url") else None,
            location=item.location,
            enabled=payload.enabled,
            # Массово найденные камеры заводятся только на запись (SPEC §1):
            # аналитика включается точечно на выбранных, иначе поиск в сети
            # из 120 устройств разом поставил бы детекцию на все.
            mode="record_only",
            status="offline",
            onvif_enabled=payload.onvif_enabled,
            onvif_host=item.host,
            onvif_port=item.port,
            onvif_username=item.username or None,
            onvif_password_enc=encrypt(item.password) if item.password else None,
        )
        db.add(cam)
        await db.commit()
        await db.refresh(cam)
        existing_hosts.add(item.host)
        added.append({"id": cam.id, "name": cam.name, "host": item.host,
                      "has_substream": bool(data.get("sub_rtsp_url"))})

    if added:
        await get_redis().publish("cameras:changed", "bulk")
    return {"added": added, "skipped": skipped, "failed": failed}


@router.get("/export")
async def export_cameras(
    format: str = Query("csv", pattern="^(csv|json)$"),
    include_secrets: bool = Query(False),
    _=Depends(require_role_query("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Выгрузка конфигурации всех камер файлом (SPEC §3).

    Ссылка открывается браузером напрямую, поэтому токен идёт в query
    string, а роль сверяется с БД (`require_role_query`), как в отчётах:
    разжалованный из админа не должен выгружать RTSP-учётки ещё 30 минут
    до истечения access-токена.

    По умолчанию пароли в RTSP-URL вырезаны — см. services/camera_config.
    Выгрузка с `include_secrets=1` пишется в журнал аудита отдельным
    действием, как и просмотр адреса одной камеры.
    """
    r = await db.execute(select(Camera).order_by(Camera.id))
    rows = [
        camera_config.camera_row(
            c,
            decrypt(c.rtsp_url_enc),
            decrypt(c.sub_rtsp_url_enc) if c.sub_rtsp_url_enc else None,
            include_secrets=include_secrets,
        )
        for c in r.scalars().all()
    ]
    if format == "json":
        body, media, name = camera_config.rows_to_json(rows), "application/json", "cameras.json"
    else:
        body, media, name = camera_config.rows_to_csv(rows), "text/csv", "cameras.csv"
    # BOM у CSV ставит общий `csv_export.encode`, а не локальный
    # "utf-8-sig": до него эта выгрузка была единственной помеченной из
    # трёх, и именно поэтому расхождение с отчётами §8 и журналом §10
    # никто не замечал. JSON остаётся без BOM — его читают парсеры, а не
    # Excel, и BOM в начале ломает строгий `json.loads`.
    return Response(
        content=csv_export.encode(body) if format == "csv" else body.encode("utf-8"),
        media_type=media,
        headers={"Content-Disposition": f"attachment; filename={name}"},
    )


def _row_to_camera_in(row: dict, existing: Camera | None) -> tuple[CameraIn, dict | None]:
    """Строка файла → провалидированный CameraIn и зоны детекции.

    Валидация RTSP-URL, режима и retention переиспользует ту же модель,
    что и веб-форма: расхождение между «что примет форма» и «что примет
    импорт» рано или поздно даёт камеру, которую воркер не сможет открыть.

    ROI возвращается отдельным значением, а не полем `CameraIn`: зоны
    правятся собственным эндпоинтом `PUT /{id}/roi` и в теле `PUT /{id}`
    не приходят. Добавь их в `CameraIn` — и сохранение формы камеры,
    которая про зоны не знает и не шлёт их, стирало бы нарисованные
    полигоны (та же мина, что уже была с полями ONVIF и расписанием).
    """
    name = str(row.get("name") or "").strip()
    if not name:
        raise ValueError("не заполнено поле name")

    main = str(row.get("rtsp_url") or "").strip()
    sub_raw = row.get("sub_rtsp_url")
    sub = str(sub_raw).strip() if sub_raw is not None else ""

    # `***` — «пароль вырезан выгрузкой»: для известной камеры значит
    # «оставить сохранённый URL», для новой подставить его неоткуда.
    if camera_config.is_masked(main) or (not main and existing):
        if not existing:
            raise ValueError(
                "в поле rtsp_url пароль вырезан выгрузкой (***), а такой камеры ещё нет — "
                "укажите полный RTSP-URL с паролем"
            )
        main = decrypt(existing.rtsp_url_enc)
    if camera_config.is_masked(sub):
        if not existing:
            raise ValueError(
                "в поле sub_rtsp_url пароль вырезан выгрузкой (***), а такой камеры ещё нет — "
                "укажите полный RTSP-URL с паролем"
            )
        sub = decrypt(existing.sub_rtsp_url_enc) if existing.sub_rtsp_url_enc else ""

    mode = str(row.get("mode") or (existing.mode if existing else "record_only")).strip() or "record_only"

    # Колонки нет в файле — сохранённое значение остаётся как есть; колонка
    # есть и пуста — значение снимается. См. модуль services/camera_config.
    if "detection_schedule" in row:
        schedule = camera_config.parse_json_object(row.get("detection_schedule"), "detection_schedule")
    else:
        schedule = existing.detection_schedule if existing else None
    if "roi" in row:
        roi = camera_config.parse_json_object(row.get("roi"), "roi")
    else:
        roi = existing.roi if existing else None
    if roi is not None:
        try:
            roi = ROIIn(**roi).model_dump()
        except ValidationError as e:
            raise ValueError("; ".join(_pydantic_messages(e))) from None

    # SPEC §6. Как и у расписания выше: колонки нет — сохранённое значение
    # не трогаем (файл прежней версии не должен выключать режим на всём
    # парке), колонка есть и пуста — режим снимается.
    record_on_motion = camera_config.parse_bool(
        row.get("record_on_motion"),
        False if existing is None else bool(existing.record_on_motion),
    )
    if record_on_motion and mode != "analytics":
        raise ValueError(
            "record_on_motion=1 требует mode=analytics: движение определяет "
            "слой аналитики, камера в режиме record_only не декодируется"
        )

    try:
        return CameraIn(
            name=name,
            rtsp_url=main,
            sub_rtsp_url=sub or None,
            location=str(row.get("location") or "").strip(),
            enabled=camera_config.parse_bool(row.get("enabled"), True if existing is None else existing.enabled),
            mode=mode,
            motion_sensitivity=camera_config.parse_int(row.get("motion_sensitivity"), "motion_sensitivity"),
            retention_days=camera_config.parse_int(row.get("retention_days"), "retention_days"),
            onvif_enabled=camera_config.parse_bool(row.get("onvif_enabled"), False if existing is None else existing.onvif_enabled),
            onvif_host=str(row.get("onvif_host") or "").strip() or None,
            onvif_port=camera_config.parse_int(row.get("onvif_port"), "onvif_port"),
            onvif_username=str(row.get("onvif_username") or "").strip() or None,
            detection_schedule=schedule,
            record_on_motion=record_on_motion,
        ), roi
    except ValidationError as e:
        raise ValueError("; ".join(_pydantic_messages(e))) from None


def _pydantic_messages(e: ValidationError) -> list[str]:
    out = []
    for err in e.errors():
        field = ".".join(str(p) for p in err.get("loc", ())) or "?"
        out.append(f"поле {field}: {err.get('msg')}")
    return out


@router.post("/import")
async def import_cameras(
    file: UploadFile = File(...),
    dry_run: bool = Query(False),
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Загрузка конфигурации камер из CSV/JSON (SPEC §3).

    **Всё или ничего.** Если хоть одна строка не прошла проверку, не
    применяется ни одна: наполовину загруженный файл на 200 камер хуже,
    чем незагруженный, — оператор не знает, до какой строки дошло, а
    повторная загрузка исправленного файла завела бы дубли. Ошибки
    возвращаются списком с номерами строк, чтобы файл можно было
    поправить целиком за один заход.

    `dry_run=1` — проверка без записи: тот же разбор и те же ошибки, но
    без изменений в БД.
    """
    content = await file.read()
    if len(content) > MAX_IMPORT_BYTES:
        raise HTTPException(413, f"файл больше {MAX_IMPORT_BYTES // 1024} КБ")
    try:
        raw_rows = camera_config.parse_file(content, file.filename or "")
    except camera_config.ImportError_ as e:
        raise HTTPException(400, str(e))
    if not raw_rows:
        raise HTTPException(400, "в файле нет ни одной камеры")
    if len(raw_rows) > MAX_IMPORT_ROWS:
        raise HTTPException(413, f"в файле больше {MAX_IMPORT_ROWS} камер")

    existing = {c.name: c for c in (await db.execute(select(Camera).order_by(Camera.id))).scalars().all()}

    errors: list[dict] = []
    planned: list[tuple[str, CameraIn, Camera | None, dict | None]] = []
    seen: set[str] = set()
    # Предел камер аналитики считается по итоговому состоянию всего файла,
    # а не по каждой строке отдельно: файл может и снимать режим analytics
    # с одних камер, и ставить на другие, и промежуточное состояние
    # посреди разбора не имеет смысла.
    analytics_after = {c.name for c in existing.values() if (c.mode or "record_only") == "analytics"}

    for idx, row in enumerate(raw_rows, start=1):
        name = str(row.get("name") or "").strip()
        prior = existing.get(name)
        try:
            if name and name in seen:
                raise ValueError(f"имя «{name}» встречается в файле дважды")
            payload, roi = _row_to_camera_in(row, prior)
        except ValueError as e:
            errors.append({"row": idx, "name": name, "error": str(e)})
            continue
        seen.add(name)
        if payload.mode == "analytics":
            analytics_after.add(name)
        else:
            analytics_after.discard(name)
        planned.append(("update" if prior else "create", payload, prior, roi))

    limit = await _analytics_limit(db)
    analytics_before = sum(1 for c in existing.values() if (c.mode or "record_only") == "analytics")
    # Блокируется только импорт, который делает хуже. Если предел уже
    # превышен (его понизили в настройках позже, чем расставили режимы),
    # файл, который аналитики не добавляет, всё равно должен применяться —
    # иначе система запирает сама себя: поправить конфигурацию файлом
    # нельзя, пока конфигурация не поправлена.
    if len(analytics_after) > limit and len(analytics_after) > analytics_before:
        errors.append({
            "row": 0,
            "name": "",
            "error": (
                f"после импорта камер в режиме аналитики стало бы {len(analytics_after)} "
                f"при пределе {limit} (настройка analytics_cameras_max)"
            ),
        })

    created = sum(1 for a, *_ in planned if a == "create")
    updated = len(planned) - created
    result = {
        "ok": not errors,
        "dry_run": dry_run,
        "total": len(raw_rows),
        "created": created if not errors else 0,
        "updated": updated if not errors else 0,
        "errors": errors,
    }
    if errors or dry_run:
        if errors:
            result["created"] = result["updated"] = 0
        return result

    for action, payload, prior, roi in planned:
        cam = prior if action == "update" else Camera(status="offline")
        cam.name = payload.name
        cam.rtsp_url_enc = encrypt(payload.rtsp_url)
        cam.sub_rtsp_url_enc = encrypt(payload.sub_rtsp_url) if payload.sub_rtsp_url else None
        cam.location = payload.location
        cam.enabled = payload.enabled
        cam.mode = payload.mode
        cam.motion_sensitivity = payload.motion_sensitivity
        cam.retention_days = payload.retention_days
        cam.onvif_enabled = payload.onvif_enabled
        cam.onvif_host = payload.onvif_host
        cam.onvif_port = payload.onvif_port
        cam.onvif_username = payload.onvif_username
        cam.detection_schedule = (payload.detection_schedule.model_dump()
                                  if payload.detection_schedule else None)
        cam.record_on_motion = payload.record_on_motion
        cam.roi = roi
        if action == "create":
            db.add(cam)
    await db.commit()
    # Один сигнал на весь файл: слой аналитики перечитывает список камер
    # целиком, и 200 публикаций подряд ему ничего не добавляют.
    await get_redis().publish("cameras:changed", "import")
    return result
