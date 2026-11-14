from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from .config import settings


class Base(DeclarativeBase):
    pass


def _connect_args(url: str) -> dict:
    """Срок на **установку** соединения с Postgres.

    Класс отказа тот же, что закрыт в воркере (`worker/liveness.py`):
    Postgres недоступен или сессия умерла молча (NAT/файрвол выбросил
    запись о соединении, сервер уехал в перезагрузку), сокет остался
    формально открытым, а операция по нему ждёт **без срока**. У бэкенда
    это выглядит не как полное зависание — цикл событий продолжает
    обслуживать остальные запросы, — а как медленное удушение: каждый
    запрос, попавший на мёртвое соединение, держит место в пуле, и через
    `pool_size + max_overflow` таких запросов интерфейс встаёт целиком.

    `timeout=10` закрывает половину задачи — установку соединения (без него
    подключение к недоступному Postgres ждёт до срока ядра, а это минуты).

    **Вторую половину — уже установленное соединение — закрыть здесь
    нечем.** psycopg2 в воркере принимает `keepalives_*` и отдаёт их libpq,
    asyncpg клиентских TCP-keepalive не выставляет вовсе (в `connect()`
    есть только `timeout`, `command_timeout` и `server_settings`; проверено
    на asyncpg 0.29). Остаётся `pool_recycle` ниже: он ограничивает,
    сколько мёртвое соединение может пролежать в пуле, но не разблокирует
    запрос, уже ушедший в мёртвый сокет.

    `command_timeout` сознательно **не** задаётся, хотя единственный он и
    оборвал бы такой запрос. Через этот же engine идут идемпотентные
    миграции старта (`lifespan`), а `CREATE INDEX` на `video_segments` в
    миллионы строк законно занимает минуты: глобальный срок убивал бы
    миграцию на большом объекте — то есть чинил бы редкий отказ, создавая
    гарантированный.

    Остаточный риск отмечен в DEPLOY_CHECKLIST как то, что проверяется на
    сервере. У воркера, где это зависание останавливало слой записи целиком,
    он закрыт с двух сторон — keepalive и сторож живости.

    Только для asyncpg: SQLite в тестах такого набора не понимает.
    """
    if "asyncpg" not in url:
        return {}
    return {"timeout": 10}


# pool_size/max_overflow подобраны под целевую нагрузку ТЗ (до 16 камер +
# несколько одновременных операторов): без явного значения SQLAlchemy
# используёт pool_size=5/max_overflow=10, что мало для короткоживущих, но
# частых запросов дашборда/стены/WS при полной загрузке камерами.
#
# pool_recycle — половина страховки от молча умерших соединений (см.
# _connect_args): соединение старше 30 минут переоткрывается, и мёртвое не
# лежит в пуле сутками. Час-полтора у SQLAlchemy по умолчанию нет вовсе —
# соединение живёт, пока его не забракует `pool_pre_ping`, а тот на
# зависшем сокете сам блокируется.
engine = create_async_engine(
    settings.DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20,
    pool_recycle=1800, connect_args=_connect_args(settings.DATABASE_URL),
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with SessionLocal() as s:
        yield s
