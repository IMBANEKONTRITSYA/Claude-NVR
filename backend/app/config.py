from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://facewatch:facewatch@postgres:5432/facewatch"
    REDIS_URL: str = "redis://redis:6379/0"
    SECRET_KEY: str = "change-me"
    RTSP_ENCRYPTION_KEY: str = "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ="
    MEDIA_PATH: str = "/media"
    ADMIN_PASSWORD: str = "admin"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 12
    WORKER_URL: str = "http://worker:9000"
    RETENTION_DAYS_DEFAULT: int = 30
    # В production фронтенд и backend живут за одним nginx (same-origin, см.
    # frontend/nginx.conf) — CORS вообще не нужен. Список нужен только для
    # локальной разработки (vite dev server на другом порту).
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    class Config:
        env_file = ".env"


settings = Settings()
