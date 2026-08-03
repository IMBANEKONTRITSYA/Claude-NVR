import os
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi import HTTPException
from jose import jwt, JWTError
from sqlalchemy import select, text
from .db import engine, Base, SessionLocal
from .models import User
from .auth import hash_password
from .config import settings
from .profiles import DEFAULT_PROFILE, profile_settings
from .routers import auth as r_auth, users as r_users, cameras as r_cameras
from .routers import persons as r_persons, events as r_events, archive as r_archive
from .routers import stats as r_stats, reports as r_reports, ws as r_ws
from .routers import search as r_search
from .routers import settings as r_settings
from .routers import audit as r_audit
from .routers import system as r_system
from .audit import AuditMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    os.makedirs(settings.MEDIA_PATH, exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "snapshots"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "segments"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "avatars"), exist_ok=True)
    os.makedirs(os.path.join(settings.MEDIA_PATH, "uploads"), exist_ok=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Лёгкие миграции для существующих БД (create_all не добавляет колонки)
        await conn.execute(text(
            "ALTER TABLE face_events ADD COLUMN IF NOT EXISTS orig_snapshot_path varchar(500)"
        ))
        await conn.execute(text(
            "ALTER TABLE face_events ADD COLUMN IF NOT EXISTS enhanced boolean DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE persons ADD COLUMN IF NOT EXISTS notes text"
        ))
        await conn.execute(text(
            "ALTER TABLE persons ADD COLUMN IF NOT EXISTS alert_on_detection boolean DEFAULT false"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS sub_rtsp_url_enc text"
        ))
        await conn.execute(text(
            "ALTER TABLE cameras ADD COLUMN IF NOT EXISTS motion_sensitivity integer"
        ))
        # HNSW-индексы pgvector для быстрого поиска по эмбеддингам (≤5с на 100k лиц)
        for stmt in (
            "CREATE INDEX IF NOT EXISTS idx_face_events_embedding ON face_events "
            "USING hnsw (embedding vector_cosine_ops)",
            "CREATE INDEX IF NOT EXISTS idx_persons_centroid ON persons "
            "USING hnsw (centroid vector_cosine_ops)",
        ):
            try:
                await conn.execute(text(stmt))
            except Exception as e:
                print(f"[startup] не удалось создать HNSW-индекс: {e}", flush=True)
    async with SessionLocal() as db:
        r = await db.execute(select(User).where(User.username == "admin"))
        if not r.scalar_one_or_none():
            db.add(User(username="admin", password_hash=hash_password(settings.ADMIN_PASSWORD), role="admin"))
            await db.commit()
        # Сид системных настроек по умолчанию
        from .models import Setting
        defaults = {
            "retention_days": str(settings.RETENTION_DAYS_DEFAULT),
            "motion_threshold": "1500",
            "similarity_threshold": "0.45",
            "event_cooldown_sec": "10",
            "telegram_bot_token": "",
            "telegram_chat_id": "",
            "alert_cooldown_sec": "300",
            "record_codec": "h264",
            "performance_profile": DEFAULT_PROFILE,
            # detection_fps, frame_skip, face_model, upscale_mode и т.д.
            **profile_settings(DEFAULT_PROFILE),
        }
        existing = {s.key for s in (await db.execute(select(Setting))).scalars().all()}
        for k, v in defaults.items():
            if k not in existing:
                db.add(Setting(key=k, value=v))
        await db.commit()
    yield


app = FastAPI(title="FaceWatch API", lifespan=lifespan)

_cors_origins = [o.strip() for o in settings.CORS_ALLOWED_ORIGINS.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    # Список источников из настроек, а не "*": в production фронтенд и API
    # ходят через один nginx (same-origin), CORS нужен только для dev-сервера.
    # Auth — Bearer-токен в заголовке (не cookie), поэтому credentials не нужны.
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(AuditMiddleware)

app.include_router(r_auth.router)
app.include_router(r_users.router)
app.include_router(r_cameras.router)
app.include_router(r_persons.router)
app.include_router(r_events.router)
app.include_router(r_archive.router)
app.include_router(r_stats.router)
app.include_router(r_reports.router)
app.include_router(r_search.router)
app.include_router(r_settings.router)
app.include_router(r_audit.router)
app.include_router(r_system.router)
app.include_router(r_ws.router)


@app.get("/api/health")
async def health():
    """Глубокая проверка: БД и Redis должны отвечать."""
    from fastapi.responses import JSONResponse
    from .services.pubsub import get_redis
    status = {"ok": True, "db": "ok", "redis": "ok"}
    try:
        async with SessionLocal() as db:
            await db.execute(text("SELECT 1"))
    except Exception as e:
        status["ok"] = False
        status["db"] = f"error: {str(e)[:80]}"
    try:
        await get_redis().ping()
    except Exception as e:
        status["ok"] = False
        status["redis"] = f"error: {str(e)[:80]}"
    return JSONResponse(status, status_code=200 if status["ok"] else 503)


@app.get("/api/media/{kind}/{name}")
async def media_file(kind: str, name: str, token: str = Query(...)):
    try:
        jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(401, "Не авторизован")
    if kind not in ("snapshots", "avatars", "segments"):
        raise HTTPException(404)
    name = os.path.basename(name)  # защита от ../ в имени
    path = os.path.join(settings.MEDIA_PATH, kind, name)
    if not os.path.exists(path):
        raise HTTPException(404)
    # Снимки иммутабельны (улучшенная версия получает новое имя enh_*),
    # так что браузер может кэшировать — Стена и галереи не перекачивают JPEG.
    return FileResponse(path, headers={"Cache-Control": "private, max-age=86400"})
