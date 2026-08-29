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

# Потолок `hnsw.ef_search` в pgvector.
_EF_SEARCH_MAX = 1000


# Запросы вынесены в константы модуля, а не собираются строкой внутри
# обработчика: тест плана (`test_integration_search_index.py`) прогоняет
# EXPLAIN именно по ним. Пока запрос жил только внутри функции, тест
# EXPLAIN'ил свою копию — и оставался зелёным при любой правке
# production-запроса, что и показала верификация откатом.

# Быстрый путь: поиск по всей базе. Порядок задан ОПЕРАТОРОМ расстояния,
# а не выражением-псевдонимом `similarity`, — только такую форму
# планировщик умеет отдать HNSW-индексу.
ANN_SEARCH_SQL = """
    WITH candidates AS (
        SELECT fe.id, fe.person_id, fe.camera_id, fe.ts, fe.snapshot_path,
               fe.embedding <=> CAST(:vec AS vector) AS distance
        FROM face_events fe
        WHERE fe.embedding IS NOT NULL
        ORDER BY fe.embedding <=> CAST(:vec AS vector)
        LIMIT :limit
    )
    SELECT c.id, c.person_id, c.camera_id, c.ts, c.snapshot_path,
           p.name, p.status,
           1 - c.distance AS similarity,
           (SELECT vs.id FROM video_segments vs
              WHERE vs.camera_id = c.camera_id
                AND c.ts >= vs.started_at AND c.ts <= vs.ended_at
              ORDER BY vs.started_at DESC LIMIT 1) AS segment_id
    FROM candidates c
    LEFT JOIN persons p ON p.id = c.person_id
    WHERE 1 - c.distance >= :threshold
    ORDER BY similarity DESC
"""

# Точный путь для суженного фильтром поиска — полный проход.
EXACT_SEARCH_SQL = """
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


def _ef_search(limit: int) -> int:
    """Сколько кандидатов HNSW просматривает на один поиск.

    Дефолт pgvector — 40, и он МЕНЬШЕ дефолтного `limit` эндпоинта (100):
    без подъёма индексный скан возвращал бы 40 строк там, где запрошено
    100, — молча урезанная выдача, которую оператор принял бы за «в архиве
    больше никого похожего нет». Замерено: на 100 000 эмбеддингов запрос с
    `LIMIT 100` отдавал ровно 40 совпадений.

    Двукратный запас против `limit` — обычная рекомендация pgvector: часть
    кандидатов отсеет порог схожести, и без запаса выдача снова окажется
    короче запрошенной.
    """
    return min(max(int(limit) * 2, 40), _EF_SEARCH_MAX)


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

    params = {"vec": vec, "threshold": threshold, "limit": limit}
    narrowed = bool(date_from or date_to or status in ("known", "unknown"))

    if not narrowed:
        # Быстрый путь: поиск по всей базе, ровно сценарий §12 («загрузка
        # фото → сравнение с базой»). Порядок задан ОПЕРАТОРОМ расстояния,
        # а не выражением-псевдонимом `similarity`, — только такую форму
        # планировщик умеет отдать HNSW-индексу.
        #
        # Замерено на 100 000 эмбеддингов (perf/bench.py --only facesearch):
        # прежний запрос — 517 мс и Seq Scan (индекс, созданный в main.py,
        # не использовался ни разу), эта форма — 2.0 мс и Index Scan.
        # Норматив §26 (≤ 3–5 с) выполнялся и раньше, но линейно по размеру
        # базы: на вдвое большем архиве прежний запрос рос бы вдвое, этот —
        # практически нет.
        await db.execute(text(f"SET LOCAL hnsw.ef_search = {_ef_search(limit)}"))
        sql = ANN_SEARCH_SQL
    else:
        # Точный путь для суженного поиска (по датам/статусу). HNSW здесь
        # не используется намеренно: индексный скан отдаёт не более
        # `ef_search` ближайших строк, фильтр применяется уже к ним, и на
        # узком фильтре результат молча оказался бы неполным — «в архиве
        # ничего нет» вместо «есть, но за пределами первых кандидатов».
        # Полный проход на 100k стоит ~0.5 с, что укладывается в §26 с
        # запасом; терять полноту ради этого нельзя.
        sql = EXACT_SEARCH_SQL
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
