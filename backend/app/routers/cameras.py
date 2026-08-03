import os
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from ..config import settings
from ..db import get_db
from ..models import Camera
from ..auth import require_role, get_current_user
from ..schemas import CameraIn, CameraOut, ROIIn, RtspTest
from ..services.encryption import encrypt, decrypt
from ..services.pubsub import get_redis

router = APIRouter(prefix="/api/cameras", tags=["cameras"])


@router.get("", response_model=list[CameraOut])
async def list_cameras(_=Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    r = await db.execute(select(Camera).order_by(Camera.id))
    return [
        CameraOut(
            id=c.id, name=c.name, location=c.location, enabled=c.enabled, status=c.status,
            has_substream=bool(c.sub_rtsp_url_enc), motion_sensitivity=c.motion_sensitivity,
        )
        for c in r.scalars().all()
    ]


@router.post("", response_model=CameraOut)
async def add_camera(payload: CameraIn, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    cam = Camera(
        name=payload.name,
        rtsp_url_enc=encrypt(payload.rtsp_url),
        sub_rtsp_url_enc=encrypt(payload.sub_rtsp_url) if payload.sub_rtsp_url else None,
        location=payload.location,
        enabled=payload.enabled,
        motion_sensitivity=payload.motion_sensitivity,
        status="offline",
    )
    db.add(cam)
    await db.commit()
    await db.refresh(cam)
    await get_redis().publish("cameras:changed", str(cam.id))
    return CameraOut(
        id=cam.id, name=cam.name, location=cam.location, enabled=cam.enabled, status=cam.status,
        has_substream=bool(cam.sub_rtsp_url_enc), motion_sensitivity=cam.motion_sensitivity,
    )


@router.put("/{cam_id}", response_model=CameraOut)
async def update_camera(cam_id: int, payload: CameraIn, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    cam = await db.get(Camera, cam_id)
    if not cam:
        raise HTTPException(404, "Камера не найдена")
    cam.name = payload.name
    cam.rtsp_url_enc = encrypt(payload.rtsp_url)
    # Пустое поле субпотока очищает его, отсутствующее — оставляет прежнее значение
    if payload.sub_rtsp_url is not None:
        cam.sub_rtsp_url_enc = encrypt(payload.sub_rtsp_url) if payload.sub_rtsp_url else None
    cam.location = payload.location
    cam.enabled = payload.enabled
    cam.motion_sensitivity = payload.motion_sensitivity
    await db.commit()
    await db.refresh(cam)
    await get_redis().publish("cameras:changed", str(cam.id))
    return CameraOut(
        id=cam.id, name=cam.name, location=cam.location, enabled=cam.enabled, status=cam.status,
        has_substream=bool(cam.sub_rtsp_url_enc), motion_sensitivity=cam.motion_sensitivity,
    )


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
    cam.roi = payload.model_dump()
    await db.commit()
    return {"ok": True}


@router.get("/{cam_id}/snapshot")
async def snapshot(cam_id: int, token: str = Query(...)):
    try:
        jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Не авторизован")
    path = os.path.join(settings.MEDIA_PATH, "snapshots", f"cam{cam_id}_latest.jpg")
    if not os.path.exists(path):
        raise HTTPException(404, "Нет кадра")
    return FileResponse(path, media_type="image/jpeg")


@router.get("/{cam_id}/hls")
async def hls_url(cam_id: int, _=Depends(get_current_user)):
    """URL HLS-плейлиста MediaMTX, прокидываемого через nginx."""
    return {"url": f"/hls/cam{cam_id}/index.m3u8"}


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
