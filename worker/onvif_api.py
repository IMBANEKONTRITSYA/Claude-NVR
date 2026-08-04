"""Внутренний HTTP-эндпоинт воркера для ONVIF-операций поверх сети камер
(ТЗ 18.7: "Поддержка ONVIF в интерфейсе: автообнаружение камер в сети,
получение профилей потоков"). Слушает тот же порт 9000, что и /embed
(embed_api.py), наружу не публикуется — проксируется бэкендом
(backend/app/routers/cameras.py, только role=admin).

Отдельный модуль от embed_api.py: тот тянет cv2/insightface (тяжёлые
зависимости, недоступные в CI worker-job — см. .github/workflows/ci.yml,
комментарий "Полный requirements.txt воркера тянет OpenCV/InsightFace/ONNX
Runtime — слишком тяжело для CI"), а этот — только fastapi и onvif_client
(чистый stdlib), поэтому тестируется в CI TestClient'ом без полного стека
воркера, отдельно от /embed, который тестами не покрыт по той же причине."""
from fastapi import APIRouter, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from onvif_client import discover_devices, get_profiles, get_stream_uri, OnvifError

router = APIRouter()


class OnvifCredentials(BaseModel):
    host: str
    port: int = 80
    username: str | None = None
    password: str | None = None


class OnvifStreamUriRequest(OnvifCredentials):
    profile_token: str


@router.get("/onvif/discover")
async def onvif_discover(timeout: float = Query(3.0, ge=0.5, le=10.0)):
    """WS-Discovery блокирует поток на до timeout секунд (ждёт UDP-ответы
    камер) — run_in_threadpool, чтобы не заморозить остальной embed API
    (в т.ч. /embed для поиска по фото) на время ручного сканирования сети
    администратором."""
    try:
        devices = await run_in_threadpool(discover_devices, timeout)
    except OnvifError as e:
        return {"ok": False, "error": str(e), "devices": []}
    return {"ok": True, "devices": devices}


@router.post("/onvif/profiles")
async def onvif_profiles(payload: OnvifCredentials):
    """Список медиа-профилей камеры (GetProfiles) — учётные данные приходят
    в теле POST, а не в query string, чтобы пароль ONVIF не оседал в
    access-логах (тот же принцип, что и redacted access-логи для ?token=,
    см. logging_utils.py — здесь проще: просто не класть секрет в URL)."""
    try:
        profiles = await run_in_threadpool(
            get_profiles, payload.host, payload.port, payload.username, payload.password,
        )
    except OnvifError as e:
        return {"ok": False, "error": str(e), "profiles": []}
    return {"ok": True, "profiles": profiles}


@router.post("/onvif/stream-uri")
async def onvif_stream_uri(payload: OnvifStreamUriRequest):
    """RTSP-адрес потока для выбранного профиля (GetStreamUri)."""
    try:
        uri = await run_in_threadpool(
            get_stream_uri, payload.host, payload.port, payload.profile_token,
            payload.username, payload.password,
        )
    except OnvifError as e:
        return {"ok": False, "error": str(e), "uri": None}
    return {"ok": True, "uri": uri}
