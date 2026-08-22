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
INSECURE_ADMIN_PASSWORDS = {
    "admin",
    "password",
    "change-me",
}


class Settings(BaseSettings):
    DATABASE_URL: str = "postgresql+asyncpg://facewatch:facewatch@postgres:5432/facewatch"
    REDIS_URL: str = "redis://redis:6379/0"
    SECRET_KEY: str = "change-me"
    RTSP_ENCRYPTION_KEY: str = "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ="
    MEDIA_PATH: str = "/media"
    ADMIN_PASSWORD: str = "admin"
    # ТЗ 13: "JWT-токены с refresh-механизмом" — короткий access-токен
    # (был 12 часов, невозможно отозвать до истечения) + долгоживущий
    # refresh-токен, который можно отозвать на сервере (logout, смена
    # пароля, обнаружение повторного использования).
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    # ТЗ 13: "парольная политика (сложность, срок действия)".
    PASSWORD_MIN_LENGTH: int = 10
    PASSWORD_MAX_AGE_DAYS: int = 90
    WORKER_URL: str = "http://worker:9000"
    RETENTION_DAYS_DEFAULT: int = 30
    # В production фронтенд и backend живут за одним nginx (same-origin, см.
    # frontend/nginx.conf) — CORS вообще не нужен. Список нужен только для
    # локальной разработки (vite dev server на другом порту).
    CORS_ALLOWED_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"
    # Аварийный люк для CI/тестов, которые импортируют приложение без .env.
    # В обычной эксплуатации не должен выставляться.
    ALLOW_INSECURE_DEFAULT_SECRETS: bool = False

    # SPEC §12: ONVIF Profile G (хранение) — сервер поиска и воспроизведения
    # записей для внешних VMS. Выключен по умолчанию: это внешняя SOAP-точка,
    # раздающая метаданные архива, и включаться она должна осознанно, с
    # заведённой ONVIF-учёткой и, желательно, за сетевой изоляцией/nginx.
    ONVIF_G_ENABLED: bool = False
    # Отдельная учётка Profile G (WS-Security UsernameToken). НЕ из таблицы
    # users: пароли пользователей — bcrypt-хэши, а UsernameToken Digest
    # требует пароль в открытом виде. Пустой пароль при включённом ONVIF_G
    # означает «фича включена, но не сконфигурирована» — сервер отвечает
    # отказом (fail-closed), а не пускает без пароля.
    ONVIF_G_USERNAME: str = "onvif"
    ONVIF_G_PASSWORD: str = ""
    # База RTSP-URI для GetReplayUri. Пусто — используется встроенный
    # replay-сервер (`services/rtsp_replay.py`), поднимаемый вместе с
    # приложением. Заполнять нужно только если перед FaceWatch стоит свой
    # прокси или порт проброшен наружу под другим адресом: угадать это за
    # оператора нельзя, а выданный VMS адрес должен быть достижим с его
    # стороны, а не с нашей.
    ONVIF_G_REPLAY_URI_BASE: str = ""
    # Встроенный RTSP-сервер воспроизведения архива (SPEC §12, Profile G
    # Replay). MediaMTX воспроизведение по абсолютному времени не умеет —
    # у него путь либо тянет камеру, либо принимает публикацию, — поэтому
    # `Range: clock=` обслуживает отдельный слушатель в процессе бэкенда.
    # Выключатель отдельный от ONVIF_G_ENABLED: поиск по записям может быть
    # нужен без раздачи самого видео наружу.
    ONVIF_G_REPLAY_BUILTIN: bool = True
    # Порт replay. 8554 занят MediaMTX (live), поэтому 8555. Наружу
    # публиковать не обязательно: VMS ходит по адресу из GetReplayUri.
    ONVIF_G_REPLAY_PORT: int = 8555
    ONVIF_G_REPLAY_HOST: str = "0.0.0.0"

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
    if s.ADMIN_PASSWORD.lower() in INSECURE_ADMIN_PASSWORDS:
        problems.append(
            "ADMIN_PASSWORD оставлен публично известным словарным значением — учётная "
            "запись администратора взламывается с первой попытки. Задайте свой пароль в .env "
            f"(минимум {s.PASSWORD_MIN_LENGTH} символов, минимум 3 из 4 классов символов)."
        )
    return problems
