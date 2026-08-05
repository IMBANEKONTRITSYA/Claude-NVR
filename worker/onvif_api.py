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

from onvif_client import (
    discover_devices,
    get_profiles,
    get_stream_uri,
    inject_credentials,
    scan_subnet,
    OnvifError,
)

router = APIRouter()


class OnvifCredentials(BaseModel):
    host: str
    port: int = 80
    username: str | None = None
    password: str | None = None


class OnvifStreamUriRequest(OnvifCredentials):
    profile_token: str


@router.get("/onvif/discover")
async def onvif_discover(
    timeout: float = Query(3.0, ge=0.5, le=10.0),
    subnet: str | None = Query(None, description="CIDR, например 192.168.1.0/24"),
):
    """Автообнаружение камер: WS-Discovery, а при указанном subnet — перебор
    адресов диапазона.

    Перебор нужен потому, что WS-Discovery рассылает multicast-датаграмму, а
    воркер живёт в docker-контейнере на NAT'ированной сети: на Docker Desktop
    под Windows multicast до физической ЛВС не доходит, и поиск честно не
    находит ничего. Перебор идёт unicast'ом и через NAT проходит.

    Оба способа блокируют поток на секунды — run_in_threadpool, чтобы не
    заморозить остальной API воркера (в т.ч. /embed для поиска по фото) на
    время ручного сканирования сети администратором.
    """
    devices: list[dict] = []
    errors: list[str] = []

    try:
        devices = await run_in_threadpool(discover_devices, timeout)
    except OnvifError as e:
        errors.append(f"WS-Discovery: {e}")

    if subnet:
        try:
            scanned = await run_in_threadpool(scan_subnet, subnet)
        except OnvifError as e:
            errors.append(str(e))
        else:
            # Один и тот же адрес мог прийти обоими путями.
            known = {d.get("host") for d in devices}
            devices += [d for d in scanned if d.get("host") not in known]

    # ok=False только когда не осталось ни одного результата: частичный сбой
    # (multicast не прошёл, а перебор нашёл камеры) — это успех для
    # пользователя, а не ошибка.
    if not devices and errors:
        return {"ok": False, "error": "; ".join(errors), "devices": []}
    return {"ok": True, "devices": devices, "warnings": errors}


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
    """RTSP-адрес потока для выбранного профиля (GetStreamUri).

    Учётные данные подставляются в возвращённый URI: камеры отдают адрес без
    них (по спецификации ONVIF они передаются отдельно), а ffmpeg/OpenCV
    читают логин и пароль только из самого URL — без подстановки адрес,
    автозаполненный в форму, сразу давал бы `401 Unauthorized` на кнопке
    «Проверить RTSP».
    """
    try:
        uri = await run_in_threadpool(
            get_stream_uri, payload.host, payload.port, payload.profile_token,
            payload.username, payload.password,
        )
    except OnvifError as e:
        return {"ok": False, "error": str(e), "uri": None}
    return {"ok": True, "uri": inject_credentials(uri, payload.username, payload.password)}
