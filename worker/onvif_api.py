"""Внутренний HTTP-эндпоинт воркера для WS-Discovery автообнаружения
ONVIF-камер в сети (ТЗ 18.7: "Поддержка ONVIF в интерфейсе: автообнаружение
камер в сети"). Слушает тот же порт 9000, что и /embed (embed_api.py),
наружу не публикуется — проксируется бэкендом (backend/app/routers/
cameras.py, только role=admin).

Отдельный модуль от embed_api.py: тот тянет cv2/insightface (тяжёлые
зависимости, недоступные в CI worker-job — см. .github/workflows/ci.yml,
комментарий "Полный requirements.txt воркера тянет OpenCV/InsightFace/ONNX
Runtime — слишком тяжело для CI"), а этот — только fastapi и onvif_client
(чистый stdlib), поэтому тестируется в CI TestClient'ом без полного стека
воркера, отдельно от /embed, который тестами не покрыт по той же причине."""
from fastapi import APIRouter, Query
from starlette.concurrency import run_in_threadpool

from onvif_client import discover_devices, OnvifError

router = APIRouter()


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
