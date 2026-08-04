from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from .config import settings


class Base(DeclarativeBase):
    pass


# pool_size/max_overflow подобраны под целевую нагрузку ТЗ (до 16 камер +
# несколько одновременных операторов): без явного значения SQLAlchemy
# используёт pool_size=5/max_overflow=10, что мало для короткоживущих, но
# частых запросов дашборда/стены/WS при полной загрузке камерами.
engine = create_async_engine(
    settings.DATABASE_URL, pool_pre_ping=True, pool_size=10, max_overflow=20,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db():
    async with SessionLocal() as s:
        yield s
