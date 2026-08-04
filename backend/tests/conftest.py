"""Общая фикстура интеграционных тестов (test_integration_*.py):
поднимает реальный жизненный цикл приложения (Base.metadata.create_all,
миграции, сид admin/settings — как в main.py:lifespan) на настоящем
Postgres+Redis, а не на моках/SQLite. Раньше обработчики роутеров почти не
покрывались тестами именно из-за отсутствия такой БД в CI (см.
docs/reviews/REVIEW_LOG.md, известные пробелы прошлых циклов).

Если Postgres недоступен (например, локальный `pytest` без docker-compose/CI
service-контейнеров) — интеграционные тесты аккуратно пропускаются, а не
роняют весь прогон."""
import os

import pytest

os.environ.setdefault("ALLOW_INSECURE_DEFAULT_SECRETS", "true")

from app.config import settings  # noqa: E402  (после setdefault выше)


def _ensure_vector_extension():
    """CREATE EXTENSION vector до старта приложения — в проде это делает
    init.sql через docker-entrypoint-initdb.d (docker-compose.yml), в CI
    поднимается «голый» сервис-контейнер Postgres без init-скриптов.
    Обычный psycopg2 (синхронный, не завязан на event loop TestClient'а)."""
    import psycopg2
    from urllib.parse import urlsplit

    # DATABASE_URL — асинхронный DSN (postgresql+asyncpg://...) для SQLAlchemy;
    # psycopg2 понимает только postgresql://.
    parsed = urlsplit(settings.DATABASE_URL.replace("+asyncpg", ""))
    conn = psycopg2.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        user=parsed.username, password=parsed.password,
        dbname=parsed.path.lstrip("/"),
        connect_timeout=5,
    )
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def client():
    """FastAPI TestClient как context manager запускает реальный lifespan
    приложения (создание таблиц, миграции, сид admin/settings) в собственном
    event loop — поэтому вся настройка БД идёт через этот же TestClient, а
    не через отдельный asyncio.run(), который создал бы другой loop и
    сломал бы пул соединений asyncpg."""
    try:
        _ensure_vector_extension()
    except Exception as e:
        pytest.skip(f"Реальный Postgres недоступен ({settings.DATABASE_URL}): {e}")

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def admin_token(client):
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": settings.ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


@pytest.fixture()
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}
