from pydantic_settings import BaseSettings

# Значения, которые ходят по умолчанию в docker-compose.yml/.env.example и
# поэтому известны каждому, кто читал публичный репозиторий. Если оператор
# не переопределил их в .env, JWT-токены подделываются тривиально, а
# RTSP-учётки "шифруются" публично известным ключом — см. INSECURE_DEFAULTS
# в CONFIG и validate_production_secrets() ниже.
INSECURE_SECRET_KEYS = {
    "change-me",
    "change-me-in-production",
    "please-change-me-to-a-long-random-string",
}
INSECURE_RTSP_ENCRYPTION_KEYS = {
    "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=",
}


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
    # Аварийный люк для CI/тестов, которые импортируют приложение без .env.
    # В обычной эксплуатации не должен выставляться.
    ALLOW_INSECURE_DEFAULT_SECRETS: bool = False

    class Config:
        env_file = ".env"


settings = Settings()


def insecure_secret_problems(s: "Settings" = settings) -> list[str]:
    """Возвращает список проблем, если критичные секреты оставлены на
    публично известных значениях по умолчанию (ТЗ 13: AES-256 для RTSP,
    защита JWT). Пустой список — секреты выглядят переопределёнными."""
    problems = []
    if s.SECRET_KEY in INSECURE_SECRET_KEYS:
        problems.append(
            "SECRET_KEY оставлен значением по умолчанию из публичного репозитория — "
            "любой сможет подделать JWT-токены. Задайте случайную строку в .env."
        )
    if s.RTSP_ENCRYPTION_KEY in INSECURE_RTSP_ENCRYPTION_KEYS:
        problems.append(
            "RTSP_ENCRYPTION_KEY оставлен значением по умолчанию из публичного репозитория — "
            "учётные данные камер расшифровываются кем угодно. Сгенерируйте свой ключ "
            "(python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\")."
        )
    return problems
