from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://facewatch:facewatch@postgres:5432/facewatch"
    REDIS_URL: str = "redis://redis:6379/0"
    SECRET_KEY: str = "change-me"
    RTSP_ENCRYPTION_KEY: str = "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ="
    MEDIA_PATH: str = "/media"
    ADMIN_PASSWORD: str = "admin"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 12

    class Config:
        env_file = ".env"


settings = Settings()
