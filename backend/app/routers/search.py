"""Поиск похожих лиц (Reverse Face Search).
Оператор загружает фото → эмбеддинг (через worker) → pgvector-поиск по архиву."""
import os
import uuid
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, UploadFile, File, Form, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from ..config import settings
from ..db import get_db
from ..auth import require_role
from ..params import limit_form, threshold_form

router = APIRouter(prefix="/api/search", tags=["search"])


async def _embed(image_bytes: bytes, filename: str) -> list[float]:
    async with httpx.AsyncClient(timeout=30) as client:
        files = {"file": (filename, image_bytes, "application/octet-stream")}
        try:
            resp = await client.post(f"{settings.WORKER_URL}/embed", files=files)
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(422, data.get("error", "Не удалось обработать фото"))
    return data["embedding"]


@router.post("/face")
async def search_face(
    file: UploadFile = File(...),
    threshold: float = threshold_form(0.4),
    date_from: datetime | None = Form(None),
    date_to: datetime | None = Form(None),
    status: str | None = Form(None),
    limit: int = limit_form(100),
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")

    # Сохраняем загруженное фото (для возможного экспорта/аудита)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "uploads"), exist_ok=True)
    up_name = f"{uuid.uuid4().hex}_{os.path.basename(file.filename or 'photo.jpg')}"
    with open(os.path.join(settings.MEDIA_PATH, "uploads", up_name), "wb") as fh:
        fh.write(content)

    embedding = await _embed(content, file.filename or "photo.jpg")
    vec = "[" + ",".join(str(x) for x in embedding) + "]"

    # Косинусная схожесть = 1 - (embedding <=> query)
    sql = """
        SELECT fe.id, fe.person_id, fe.camera_id, fe.ts, fe.snapshot_path,
               p.name, p.status,
               1 - (fe.embedding <=> CAST(:vec AS vector)) AS similarity,
               (SELECT vs.id FROM video_segments vs
                  WHERE vs.camera_id = fe.camera_id
                    AND fe.ts >= vs.started_at AND fe.ts <= vs.ended_at
                  ORDER BY vs.started_at DESC LIMIT 1) AS segment_id
        FROM face_events fe
        LEFT JOIN persons p ON p.id = fe.person_id
        WHERE fe.embedding IS NOT NULL
          AND (1 - (fe.embedding <=> CAST(:vec AS vector))) >= :threshold
    """
    params = {"vec": vec, "threshold": threshold, "limit": limit}
    if date_from:
        sql += " AND fe.ts >= :date_from"
        params["date_from"] = date_from
    if date_to:
        sql += " AND fe.ts <= :date_to"
        params["date_to"] = date_to
    if status in ("known", "unknown"):
        sql += " AND p.status = :status"
        params["status"] = status
    sql += " ORDER BY similarity DESC LIMIT :limit"

    rows = (await db.execute(text(sql), params)).all()
    return [
        {
            "event_id": r[0],
            "person_id": r[1],
            "camera_id": r[2],
            "ts": r[3].isoformat() if r[3] else None,
            "snapshot_path": r[4],
            "name": (r[5] if (r[5] and r[5].strip()) else f"Неизвестный #{r[1]}"),
            "status": r[6],
            "similarity": round(float(r[7]), 4),
            "segment_id": r[8],
        }
        for r in rows
    ]
