import json
import os
import uuid
import httpx
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete, update, func, text
from ..config import settings
from ..db import get_db
from ..models import Person, FaceEvent
from ..auth import require_role
from ..schemas import PersonOut, PersonUpdate
from ..pagination import PageParams
from ..params import limit_param
from ..services.biometrics import erase_person
from ..services.pubsub import get_redis
from ..services.person_tags import TagError, merge_tags, normalize_tag, normalize_tags

router = APIRouter(prefix="/api/persons", tags=["persons"])


@router.get("")
async def list_persons(
    status: str | None = None,
    q: str | None = None,
    tag: str | None = None,
    page: PageParams = Depends(),
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    base = select(Person)
    if status:
        base = base.where(Person.status == status)
    if q:
        base = base.where(Person.name.ilike(f"%{q}%"))
    if tag:
        # Значение приводится к тому же каноническому виду, что и на записи
        # (services/person_tags.py): иначе фильтр по «VIP» не нашёл бы
        # персону с тегом «vip» и показал бы пустой список вместо ошибки.
        # `contains` — это `tags @> ARRAY[...]`, то есть идёт по GIN-индексу.
        canon = normalize_tag(tag)
        if canon:
            base = base.where(Person.tags.contains([canon]))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar() or 0
    rows = (await db.execute(
        base.order_by(Person.id.desc()).limit(page.page_size).offset(page.offset)
    )).scalars().all()
    return {
        "items": [
            {
                "id": p.id, "name": p.name, "status": p.status,
                "avatar_path": p.avatar_path,
                "notes": p.notes, "tags": list(p.tags or []),
                "alert_on_detection": p.alert_on_detection,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in rows
        ],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
    }


@router.post("")
async def create_person(
    name: str = Form(...),
    file: UploadFile = File(...),
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """Ручное добавление известной персоны по фото (watchlist-сценарий)."""
    if not name.strip():
        raise HTTPException(400, "Имя обязательно")
    content = await file.read()
    if not content:
        raise HTTPException(400, "Пустой файл")

    # Получаем эмбеддинг через воркер (embed-API)
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(
                f"{settings.WORKER_URL}/embed",
                files={"file": (file.filename or "photo.jpg", content, "application/octet-stream")},
            )
        except httpx.HTTPError as e:
            raise HTTPException(503, f"Сервис распознавания недоступен: {e}")
    data = resp.json()
    if not data.get("ok"):
        raise HTTPException(422, data.get("error", "Не удалось обработать фото"))
    embedding = data["embedding"]

    # Сохраняем аватар
    avatars_dir = os.path.join(settings.MEDIA_PATH, "avatars")
    os.makedirs(avatars_dir, exist_ok=True)
    avatar_name = f"manual_{uuid.uuid4().hex}.jpg"
    with open(os.path.join(avatars_dir, avatar_name), "wb") as fh:
        fh.write(content)
    avatar_rel = f"avatars/{avatar_name}"

    # Создаём персону с центроидом через сырой SQL (Vector через ORM требует pgvector-адаптер на каждой сессии).
    # alert_on_detection указан явно: у колонки есть только Python-side default в модели
    # (Boolean, default=False), а не server_default — сырой INSERT его не подхватывает
    # и раньше падал с NotNullViolationError на каждом вызове (см. REVIEW_LOG.md, цикл 5).
    res = await db.execute(text(
        "INSERT INTO persons (name, status, avatar_path, centroid, alert_on_detection, created_at) "
        "VALUES (:n, 'known', :a, CAST(:c AS vector), false, NOW()) RETURNING id"
    ), {"n": name.strip(), "a": avatar_rel, "c": "[" + ",".join(str(x) for x in embedding) + "]"})
    pid = res.scalar()
    await db.commit()
    return {"id": pid, "name": name.strip(), "status": "known", "avatar_path": avatar_rel}


# Объявлен ДО `/{pid}`: FastAPI разбирает роуты в порядке объявления, и
# после `/{pid}` этот путь ушёл бы в него как pid="tags" — то есть 422
# вместо справочника. Порядок закреплён тестом.
@router.get("/tags")
async def list_tags(
    _=Depends(require_role("admin", "operator")),
    db: AsyncSession = Depends(get_db),
):
    """Справочник тегов с числом персон на каждый (SPEC §15).

    Нужен интерфейсу дважды: выпадающим фильтром и подсказкой при вводе.
    Без него оператор на объекте набирает тег заново каждый раз и получает
    «подрядчик» и «подрядчики» как два разных — та самая рассинхронизация,
    ради которой в модуле нормализации сделан нижний регистр.
    """
    rows = (await db.execute(text(
        "SELECT t AS tag, COUNT(*) AS count "
        "FROM persons, unnest(tags) AS t "
        "GROUP BY t ORDER BY COUNT(*) DESC, t"
    ))).all()
    return [{"tag": r[0], "count": r[1]} for r in rows]


@router.get("/{pid}", response_model=PersonOut)
async def get_person(pid: int, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    p = await db.get(Person, pid)
    if not p:
        raise HTTPException(404, "Персона не найдена")
    return p


@router.patch("/{pid}", response_model=PersonOut)
async def update_person(pid: int, payload: PersonUpdate, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    p = await db.get(Person, pid)
    if not p:
        raise HTTPException(404, "Персона не найдена")
    if payload.name is not None:
        p.name = payload.name
        if payload.name.strip():
            p.status = "known"
    if payload.status is not None:
        p.status = payload.status
    if payload.notes is not None:
        p.notes = payload.notes
    if payload.tags is not None:
        try:
            p.tags = normalize_tags(payload.tags)
        except TagError as e:
            # 400 с текстом, а не усечение: см. person_tags.py, решение 2.
            raise HTTPException(400, str(e))
    if payload.alert_on_detection is not None:
        p.alert_on_detection = payload.alert_on_detection
    await db.commit()
    await db.refresh(p)
    return p


@router.delete("/{pid}")
async def delete_person(pid: int, _=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    """Снимает карточку. События остаются в архиве, но без владельца.

    Это НЕ удаление биометрии: внешний ключ `face_events.person_id` объявлен
    `ON DELETE SET NULL`, поэтому снимки лица и эмбеддинги остаются в базе и
    остаются находимыми поиском по фото. Требование §24 «удаление данных по
    требованию» закрывает соседний роут `/{pid}/biometrics`.
    """
    await db.execute(delete(Person).where(Person.id == pid))
    await db.commit()
    return {"ok": True}


@router.delete("/{pid}/biometrics")
async def erase_person_biometrics(
    pid: int,
    _=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    """Удаление биометрии человека по требованию (SPEC §24, 152-ФЗ).

    Сносит карточку вместе с её событиями, эмбеддингами и файлами снимков —
    в отличие от `DELETE /{pid}`, который оставляет всё это в базе. Только
    admin: операция необратима и затрагивает архив, а не одну карточку.
    """
    result = await erase_person(db, settings.MEDIA_PATH, pid)
    if result is None:
        raise HTTPException(404, "Персона не найдена")
    return {"ok": True, **result}


@router.post("/{src_id}/merge/{dst_id}")
async def merge_persons(src_id: int, dst_id: int, _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    if src_id == dst_id:
        raise HTTPException(400, "Нельзя слить с самой собой")
    # Теги источника переезжают к цели до его удаления (SPEC §15). Оператор
    # размечает обе карточки задолго до того, как поймёт, что это один
    # человек: без объединения слияние молча стирало бы половину разметки,
    # и заметно это стало бы на фильтре, а не в момент действия.
    src = await db.get(Person, src_id)
    dst = await db.get(Person, dst_id)
    if src is not None and dst is not None:
        dst.tags = merge_tags(dst.tags, src.tags)
    await db.execute(update(FaceEvent).where(FaceEvent.person_id == src_id).values(person_id=dst_id))
    await db.execute(delete(Person).where(Person.id == src_id))
    await db.commit()
    return {"ok": True}


@router.get("/{pid}/gallery", response_model=list[dict])
async def gallery(pid: int, limit: int = limit_param(50), _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    r = await db.execute(
        select(FaceEvent.id, FaceEvent.ts, FaceEvent.snapshot_path, FaceEvent.camera_id, FaceEvent.enhanced)
        .where(FaceEvent.person_id == pid)
        .order_by(FaceEvent.ts.desc())
        .limit(limit)
    )
    return [{"id": x[0], "ts": x[1], "snapshot_path": x[2], "camera_id": x[3], "enhanced": x[4]} for x in r.all()]


@router.post("/{pid}/enhance")
async def enhance_person(pid: int, limit: int = limit_param(20), _=Depends(require_role("admin", "operator")), db: AsyncSession = Depends(get_db)):
    """Принудительный нейросетевой апскейл снимков персоны (ставит в очередь)."""
    person = await db.get(Person, pid)
    if not person:
        raise HTTPException(404, "Персона не найдена")
    r = await db.execute(
        select(FaceEvent.id).where(FaceEvent.person_id == pid).order_by(FaceEvent.ts.desc()).limit(limit)
    )
    ids = [x[0] for x in r.all()]
    redis = get_redis()
    for eid in ids:
        await redis.lpush("upscale:queue", json.dumps({"event_id": eid, "force": True}))
    return {"ok": True, "queued": len(ids)}
