"""Собственная БД под тесты апскейла — изоляция от схемы бэкенда.

Проблема (carryover цикла 21, найдено циклом 23 в измеримом виде). Тесты
апскейла вызывали `Base.metadata.create_all(engine)` на той БД, что указана
в `DATABASE_URL`. Модель апскейла — намеренно урезанная: ему нужны только
`id/camera_id/person_id/ts/snapshot_path` из `face_events` и пара полей из
`persons`, никаких `embedding`, `users`, `cameras`. На пустой БД
`create_all` создавал по этой урезанной модели одноимённые таблицы, и
дальше бэкенд на той же БД видел их существующими: `create_all` не
добавляет недостающие колонки, а `ALTER TABLE ... ADD COLUMN` бэкенда
покрывает не все из них.

В CI это не видно — у джоб `backend` и `upscaler` разные сервис-контейнеры.
Ломался локальный прогон по одной БД, и ломался обманчиво: измерено на
цикле 23 — прогон `upscaler/tests` перед `backend/tests` давал
**116 passed, 138 errors** вместо 254 passed, причём почти все ошибки
выглядели как `relation "users" does not exist`, то есть указывали куда
угодно, только не на апскейл.

(Часть тех 138 ошибок была вторым, независимым дефектом — откатом всего
блока стартовых миграций бэкенда из-за упавшего HNSW-индекса; он закрыт
отдельно, в PR #66. После него оставался 21 честный отказ из-за урезанной
схемы — их и убирает этот файл.)

Решение: тесты апскейла работают на **собственной БД**, создаваемой на
время прогона. Не на схеме внутри общей БД: `create_all` проверяет
существование таблиц через `search_path` и при `search_path = scratch,
public` нашёл бы таблицы бэкенда в `public` — изоляция вышла бы мнимой,
причём молча.

Побочный эффект, ради которого это стоило делать и само по себе: прогон
перестал зависеть от того, развёрнута ли уже схема бэкенда, и от порядка
джоб/прогонов. Раньше `create_all` на общей БД был ещё и обязательным
шагом — теперь он выполняется в заведомо пустой БД, где его семантика
однозначна.
"""
import os
import uuid

import pytest

if not os.environ.get("DATABASE_URL"):
    pytest.skip("нет DATABASE_URL — апскейл требует БД", allow_module_level=True)

upscaler = pytest.importorskip(
    "upscaler",
    reason="нужны зависимости апскейла (cv2/numpy/sqlalchemy/redis)",
)

import sqlalchemy  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def dedicated_db():
    """Временная БД на прогон; `upscaler.engine`/`Session` смотрят в неё.

    `autouse` + область `session`: подмена должна произойти до любого теста,
    иначе часть из них успеет сходить в общую БД.

    Пропуск, а не падение, если Postgres недоступен или нет права
    `CREATE DATABASE`, — локальный прогон без docker-compose не должен
    давать ложное «failed» на здоровом дереве (урок цикла 18).
    """
    admin_url = os.environ["DATABASE_URL"]
    name = f"fw_upscaler_{uuid.uuid4().hex[:12]}"
    admin = sqlalchemy.create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as c:
            c.execute(sqlalchemy.text(f'CREATE DATABASE "{name}"'))
    except Exception as e:
        admin.dispose()
        pytest.skip(f"нет живого Postgres или права CREATE DATABASE: {e}")

    url = admin_url.rsplit("/", 1)[0] + f"/{name}"
    # Через make_engine — те же параметры пула, что у сервиса в проде;
    # test_service_lifecycle.py измеряет именно их.
    engine = upscaler.make_engine(url)
    orig = (upscaler.engine, upscaler.Session, upscaler.DATABASE_URL)
    upscaler.engine = engine
    upscaler.Session = sessionmaker(bind=engine)
    # DATABASE_URL тоже: тесты удержания сессии открывают по нему отдельное
    # «наблюдательное» соединение (взгляд со стороны, как у мониторинга в
    # проде) — оно должно смотреть в ту же БД.
    upscaler.DATABASE_URL = url
    try:
        upscaler.Base.metadata.create_all(engine)
        yield engine
    finally:
        upscaler.engine, upscaler.Session, upscaler.DATABASE_URL = orig
        engine.dispose()
        with admin.connect() as c:
            c.execute(sqlalchemy.text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
