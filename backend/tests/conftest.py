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
# Фоновый цикл планировщика отчётов тикает по стенным часам и закрывает
# расписания с наступившим слотом (по умолчанию 8:00). В прогоне после
# 8 утра это гонка с тестами, которые заводят включённое расписание и
# проверяют его слот. Логика прохода покрыта явными `run_due_now()`
# (test_integration_report_schedules.py), поэтому фоновый цикл в тестах
# гасится — прогон становится детерминированным. См. main.py:lifespan.
os.environ.setdefault("FACEWATCH_DISABLE_REPORT_SCHEDULER", "true")

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


@pytest.fixture(autouse=True)
def _clear_rate_limit_counters():
    """Снимает счётчики brute-force перед каждым тестом.

    Счётчики живут в Redis по ключу, привязанному к **id пользователя**
    (`changepw_fail:{id}`) или к паре IP+логин (`login_fail:{ip}:{name}`), и
    переживают тест: TTL — 5 минут (routers/auth.py). Postgres между
    прогонами пересоздаётся, поэтому id начинают выдаваться заново, и
    пользователь нового прогона получает счётчик пользователя предыдущего —
    тест на блокировку падает с 429 там, где ожидал 400, причём только в
    полном прогоне и только если предыдущий был меньше пяти минут назад.

    Это carryover-пункт цикла 23 («кросс-сервисное загрязнение Redis
    тестами»), воспроизведённый в цикле 24 на другом ключе. Чистятся
    именно два префикса, а не `FLUSHDB`: `REDIS_URL` в песочнице может
    указывать на ту же базу, где лежит очередь апскейла и данные разработки.
    """
    import redis as sync_redis

    try:
        r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
        for prefix in ("login_fail:*", "changepw_fail:*"):
            keys = list(r.scan_iter(match=prefix, count=500))
            if keys:
                r.delete(*keys)
        r.close()
    except Exception:
        # Redis недоступен — интеграционные тесты и так пропустятся,
        # а юнит-тесты счётчиков не касаются.
        pass
    yield


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
def make_camera(client, admin_headers):
    """Заводит камеру через API и **удаляет её после теста**.

    Возвращает функцию `(name, **поля CameraIn) -> dict` (тело ответа
    `CameraOut`). Парная к `make_user` и по той же причине: до цикла 24
    камеры заводились прямо в теле тестов и не убирались за собой, поэтому
    повторный локальный прогон по той же БД копил их десятками. В CI это
    не видно — там свежий сервис-контейнер на каждый прогон.

    До появления режима камеры (SPEC §2) протёкшие камеры ничего не ломали,
    поэтому пробел и жил. С цикла 24 ломают: число камер в режиме
    `analytics` ограничено настройкой, и накопленные чужие камеры съедают
    предел, из-за чего падает не тот тест, который протёк.
    """
    created = []

    def _make(name: str, **fields):
        payload = {"name": name, "rtsp_url": f"rtsp://cam/{name}", **fields}
        r = client.post("/api/cameras", json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        cam = r.json()
        created.append(cam["id"])
        return cam

    # Для камер, созданных в обход фикстуры (например, POST, от которого
    # ожидался отказ, а он неожиданно прошёл): взять на уборку постфактум,
    # чтобы упавший тест ронял себя, а не следующий.
    _make.adopt = created.append

    yield _make

    for cam_id in created:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


@pytest.fixture()
def cam_residue(client, admin_headers):
    """Убирает камеры, появившиеся за время теста.

    Парная к `make_camera`, но для камер, id которых тест заранее не
    знает: их заводит импорт конфигурации (SPEC §3) или запрос, от
    которого ожидался отказ. Снимок «что было до» и удаление всего нового
    покрывает оба случая — иначе упавший тест оставляет камеры в БД и
    роняет следующий прогон (сторож test_zz_suite_leaves_no_residue.py,
    урок цикла 21).

    Жила в test_camera_config_io.py до цикла 36; поднята в conftest, когда
    понадобилась второму файлу.
    """
    def _ids():
        return {c["id"] for c in client.get("/api/cameras", headers=admin_headers).json()}

    before = _ids()
    yield
    for cam_id in _ids() - before:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


@pytest.fixture()
def make_user_headers(make_user):
    """`(username, role) -> {"Authorization": "Bearer ..."}` — самый частый
    способ использования `make_user` в тестах матрицы прав."""
    def _make(username: str, role: str):
        _, token = make_user(username, role)
        return {"Authorization": f"Bearer {token}"}
    return _make


def _settings_snapshot(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        return dict(cur.fetchall())


@pytest.fixture()
def restore_settings(pg_conn):
    """Возвращает таблицу `settings` ровно в то состояние, что была до теста.

    Настройки — единственное общее изменяемое состояние, которое переживает
    тест и при этом не является ни пользователем, ни камерой, то есть не
    покрыто сторожами `test_zz_suite_leaves_no_residue.py`. Протекают они
    так же неприятно: оставленный `performance_profile = economy` меняет
    поведение тестов профилей, а `analytics_cameras_max = 1` — тестов
    режима камеры (падает при этом не тот тест, который протёк).

    Восстановление идёт сырым SQL, а не через API, по двум причинам.
    Первая: `POST /api/settings/profile/{name}` не умеет вернуть состояние
    `custom` — профиль, подправленный руками, он затирает. Вторая: ключи,
    которых нет в `SCHEMA` роутера настроек (например, отметка
    `autoconfig_applied_at`), через API не удаляются вовсе.
    """
    before = _settings_snapshot(pg_conn)
    yield
    with pg_conn.cursor() as cur:
        cur.execute("SELECT key FROM settings")
        after_keys = {row[0] for row in cur.fetchall()}
        for key, value in before.items():
            cur.execute("UPDATE settings SET value = %s WHERE key = %s", (value, key))
        for key in after_keys - set(before):
            cur.execute("DELETE FROM settings WHERE key = %s", (key,))


@pytest.fixture(scope="session")
def settings_baseline(client) -> dict[str, str]:
    """Снимок настроек на старте прогона — база для сторожа остатка.

    Session-scoped и зависит от `client`, поэтому снимается после сида
    `main.py:lifespan`, но до тела первого теста. Именно этот снимок
    сравнивает `test_zz_suite_leaves_no_residue.py`.
    """
    import psycopg2
    from urllib.parse import urlsplit

    parsed = urlsplit(settings.DATABASE_URL.replace("+asyncpg", ""))
    conn = psycopg2.connect(
        host=parsed.hostname, port=parsed.port or 5432,
        user=parsed.username, password=parsed.password,
        dbname=parsed.path.lstrip("/"), connect_timeout=5,
    )
    conn.autocommit = True
    try:
        return _settings_snapshot(conn)
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _pin_settings_baseline(settings_baseline):
    """Снимок обязан сниматься на первом же тесте, а не на том, который
    первым его запросил, — иначе базой станут уже испорченные настройки."""
    return settings_baseline
