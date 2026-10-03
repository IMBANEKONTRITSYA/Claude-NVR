"""Стартовые миграции схемы: отказ создания индекса не должен откатывать блок.

Тест идёт production path: настоящий Postgres, настоящая
`app.main.apply_schema_migrations()` — та самая функция, которую вызывает
`lifespan` при каждом старте бэкенда, без единого мока. Отдельная временная
БД на каждый тест — то есть ровно та ситуация, что при первом развёртывании.

Что проверяется и почему это не синтетика (цикл 23). Весь блок миграций
выполняется одной транзакцией внутри `engine.begin()`. Создание
HNSW-индексов в конце блока обёрнуто в `try/except` осознанно — индекс это
ускорение, без него приложение работает. Но в Postgres упавший оператор
переводит транзакцию в состояние aborted, и перехват исключения на стороне
Python этого не отменяет: до конца блока не выполнится ни один следующий
оператор, а на выходе откатится **всё**, включая `create_all` и все
`ALTER TABLE`. То есть `except` не смягчал отказ, а превращал его из
«нет индекса» в «нет схемы» — и оставлял в логе одно предупреждение про
индекс как единственный след.

Два реальных сценария отказа `CREATE INDEX ... USING hnsw`:

* pgvector < 0.5.0 (метод доступа `hnsw` появился в 0.5.0) — на такой БД
  оператор падает с `access method "hnsw" does not exist`; это ровно тот
  случай, ради которого `except` здесь и стоит;
* таблица `face_events`/`persons` уже существует, но без колонки
  `embedding`/`centroid` — так получается, если БД делит инстанс с сервисом
  апскейла, чья урезанная модель создаёт одноимённые таблицы.

Последствия различались по типу БД, и вторая половина — тише и хуже:

* пустая БД: откатывается `create_all`, приложение падает на сиде админа с
  `relation "users" does not exist`;
* уже развёрнутая БД: таблицы переживают откат (они были созданы раньше),
  приложение стартует нормально — но **все `ALTER TABLE ... ADD COLUMN`
  этого блока молча не применяются**. Обновление версии, добавляющее
  колонку, выглядит успешным и отдаёт 500 на первом же запросе к ней.

Временная БД, а не отдельная схема в тестовой: `create_all` проверяет
существование таблиц через `search_path`, и при `search_path = scratch,
public` он находит `users`/`cameras` в `public` тестовой БД и не создаёт
их в scratch — проверка стала бы бессмысленной, причём молча зелёной.
"""
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import settings
from app.main import apply_schema_migrations

# anyio, а не pytest-asyncio: в CI ставится только `requirements.txt` +
# pytest/pytest-cov/aiosqlite, и `@pytest.mark.asyncio` там молча не
# собирался бы. Та же связка, что в `test_auth_refresh.py`.
pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _admin_url():
    """DSN серверной БД, из которой создаётся/удаляется временная."""
    return settings.DATABASE_URL


@pytest.fixture
async def fresh_db():
    """Пустая временная БД с расширением `vector`; отдаёт async-движок к ней.

    Пропуск, а не падение, если Postgres недоступен или у пользователя нет
    права `CREATE DATABASE`: локальный прогон без docker-compose не должен
    давать ложное «failed» на здоровом дереве (урок цикла 18).
    """
    name = f"fw_migr_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(_admin_url(), isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    except Exception as e:
        await admin.dispose()
        pytest.skip(f"нет живого Postgres или права CREATE DATABASE: {e}")

    url = _admin_url().rsplit("/", 1)[0] + f"/{name}"
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        yield engine
    finally:
        await engine.dispose()
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        await admin.dispose()


async def _tables(conn):
    r = await conn.execute(text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    ))
    return set(r.scalars().all())


async def _columns(conn, table):
    r = await conn.execute(text(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table})
    return set(r.scalars().all())


async def test_failing_index_does_not_roll_back_migrations(fresh_db):
    """Урезанная `face_events` без `embedding` ⇒ HNSW-индекс падает.

    Ожидание: остальной блок применён — таблица `users` создана (иначе
    приложение не поднимется), а колонка `cameras.onvif_host`, добавляемая
    `ALTER TABLE` уже после `create_all`, присутствует. До фикса
    откатывалось и то и другое, а в логе оставалось предупреждение
    только про индекс.
    """
    async with fresh_db.begin() as conn:
        # Урезанная копия таблицы — как её создаёт модель сервиса апскейла:
        # без `embedding`, поэтому CREATE INDEX ... hnsw (embedding) упадёт.
        await conn.execute(text(
            "CREATE TABLE face_events (id serial PRIMARY KEY, snapshot_path varchar)"
        ))

    async with fresh_db.begin() as conn:
        await apply_schema_migrations(conn)

    # Отдельным соединением после коммита: проверяется не состояние внутри
    # транзакции, а то, что осталось в БД — именно оно и откатывалось.
    async with fresh_db.connect() as conn:
        tables = await _tables(conn)
        assert "users" in tables, (
            "таблица users не создана — блок миграций откатился целиком "
            f"(в БД только: {sorted(tables)})"
        )
        cameras = await _columns(conn, "cameras")
        assert "onvif_host" in cameras, (
            "миграция ALTER TABLE cameras откатилась вместе с упавшим индексом"
        )


async def test_indexes_created_on_healthy_schema(fresh_db):
    """Контрольный случай: на чистой БД оба HNSW-индекса создаются.

    Без него первый тест прошёл бы и на реализации, которая просто
    перестала создавать индексы вовсе.
    """
    async with fresh_db.begin() as conn:
        await apply_schema_migrations(conn)

    async with fresh_db.connect() as conn:
        r = await conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
        ))
        names = set(r.scalars().all())
    assert "idx_face_events_embedding" in names
    assert "idx_persons_centroid" in names


async def test_migrations_are_idempotent(fresh_db):
    """Повторный прогон на уже мигрированной БД проходит без ошибок.

    `lifespan` выполняет этот блок при **каждом** старте контейнера, то есть
    второй прогон — штатный режим, а не крайний случай.
    """
    async with fresh_db.begin() as conn:
        await apply_schema_migrations(conn)
    async with fresh_db.begin() as conn:
        await apply_schema_migrations(conn)

    async with fresh_db.connect() as conn:
        assert "users" in await _tables(conn)
