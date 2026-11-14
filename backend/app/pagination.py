"""Серверная пагинация: возвращает {items, total, page, page_size}.
Параметры page (>=1) и page_size (1..200) валидируются единообразно."""
from fastapi import Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession


class PageParams:
    def __init__(self, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)):
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


async def paginate(db: AsyncSession, base_query, count_column, page: PageParams, mapper):
    """Выполняет COUNT(...) по тому же фильтру и SELECT с LIMIT/OFFSET."""
    total = (await db.execute(select(func.count(count_column)).select_from(base_query.subquery()))).scalar() or 0
    q = base_query.limit(page.page_size).offset(page.offset)
    rows = (await db.execute(q)).all()
    return {
        "items": [mapper(r) for r in rows],
        "total": total,
        "page": page.page,
        "page_size": page.page_size,
    }
