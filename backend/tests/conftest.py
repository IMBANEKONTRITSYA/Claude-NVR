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


@pytest.fixture()
def pg_conn():
    """Синхронное сырое подключение к тому же Postgres, что и приложение —
    для сидинга строк (persons/face_events/cameras с pgvector-колонками),
    которые нельзя создать через API без живого worker'а (эмбеддинги в CI
    не считаются — сервис распознавания в CI не поднимается, см.
    .github/workflows/ci.yml). Каждый тест получает и коммитит свои строки
    сам и сам же их подчищает — фикстура только даёт соединение."""
    import psycopg2
    from urllib.parse import urlsplit

    parsed = urlsplit(settings.DATABASE_URL.replace("+asyncpg", ""))
    conn = psycopg2.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        user=parsed.username, password=parsed.password,
        dbname=parsed.path.lstrip("/"),
        connect_timeout=5,
    )
    conn.autocommit = True
    try:
        yield conn
    finally:
        conn.close()


TEST_USER_PASSWORD = "Str0ngPass!23"


@pytest.fixture()
def make_user(client, admin_headers):
    """Заводит пользователя с нужной ролью и **удаляет его после теста**.

    Возвращает функцию `(username, role) -> (user_id, access_token)`.
    Пользователи создаются и удаляются через настоящий API, поэтому в БД
    оказывается ровно то, что оказалось бы в проде, — а не строка,
    вставленная в обход валидации и хеширования пароля.

    Фикстура вынесена сюда в цикле 21, чтобы закрыть известный пробел
    «изоляция тестов бэкенда» (carryover циклов 19-20). До этого она
    существовала в двух копиях (`test_media_rbac.py`,
    `test_query_token_identity.py`), а ещё семь тестов в пяти файлах
    заводили пользователей прямо в теле и **не убирали их за собой**.
    В CI это не видно — там каждый прогон получает свежие
    сервис-контейнеры, — но повторный локальный прогон по той же БД падал:
    `POST /api/users` на существующем имени отвечает 400, и падал не тот
    тест, который «протёк», а следующий за ним. Замерено в цикле 21: на
    не сброшенной между прогонами БД 8 падений в `test_integration_*` на
    полностью здоровом дереве.

    Имя пользователя стоит делать уникальным на тест (`request.node.name`),
    даже с уборкой: тест, упавший до финализатора, иначе отравит соседей.
    """
    created = []

    def _make(username: str, role: str, password: str = TEST_USER_PASSWORD):
        r = client.post(
            "/api/users",
            json={"username": username, "password": password, "role": role},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        user_id = r.json()["id"]
        created.append(user_id)
        lr = client.post("/api/auth/login", data={"username": username, "password": password})
        assert lr.status_code == 200, lr.text
        return user_id, lr.json()["access_token"]

    yield _make

    for user_id in created:
        client.delete(f"/api/users/{user_id}", headers=admin_headers)


@pytest.fixture()
def make_user_headers(make_user):
    """`(username, role) -> {"Authorization": "Bearer ..."}` — самый частый
    способ использования `make_user` в тестах матрицы прав."""
    def _make(username: str, role: str):
        _, token = make_user(username, role)
        return {"Authorization": f"Bearer {token}"}
    return _make
