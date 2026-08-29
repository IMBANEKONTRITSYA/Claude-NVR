"""Границы числовых query/form-параметров — в одном месте, а не по вкусу
каждого роутера.

Цикл 21 нашёл класс: двенадцать эндпоинтов принимали `days`/`limit`/
`threshold` как голый `int`/`float` без `ge`/`le`, то есть значение
пользователя уходило в `timedelta(days=...)` и в `LIMIT` как есть.
Проверено на живом приложении (реальные Postgres+pgvector+Redis, не моки):

    GET /api/stats/by-day?days=999999999      → OverflowError: date value out of range
    GET /api/stats/heatmap?days=99999999999999→ OverflowError: Python int too large to convert to C int
    GET /api/stats/top-persons?limit=-1       → InvalidRowCountInLimitClauseError: LIMIT must not be negative
    GET /api/archive/segments?limit=-1        → то же
    GET /api/persons/{id}/gallery?limit=-1    → то же
    GET /api/reports/appearances.csv?days=999999999 → OverflowError

— все шесть необработанные 500, а не 422. Три из них (`/api/stats/*`)
доступны роли «наблюдатель»: по матрице прав ТЗ это самая низкая роль,
и ей достаточно одного GET, чтобы получить трейс в логе вместо ответа.

Вторая половина проблемы — не отказ, а расход. Без верхней границы:
`POST /api/persons/{id}/enhance?limit=<много>` ставит в `upscale:queue`
столько задач, сколько у персоны событий, минуя ограничение в 500
элементов, которое воркер соблюдает при штатной постановке
(`worker.py`: `if want and r.llen("upscale:queue") < 500`) — очередь
Redis растёт неограниченно, а апскейл занят ей часами.
`GET /api/archive/segments?limit=100000000` и
`POST /api/search/face` с `threshold=-1` вытягивают в память процесса
результат без потолка.

Значения ниже подобраны так, чтобы фронтенд не изменился: слайдер порога
на странице поиска ходит 0.1..0.9, поле «дней» в отчётах ограничено 1..365,
списки запрашиваются с дефолтами.
"""
from fastapi import Form, Query

# Верхняя граница окна выборки. Больше, чем любой мыслимый срок хранения
# (`retention_days` в настройках), и заведомо меньше того, на чём
# `datetime.utcnow() - timedelta(days=...)` уходит в OverflowError.
MAX_DAYS = 3650

# Потолок размера ответа-списка. Совпадает с `page_size` серверной
# пагинации (pagination.PageParams: le=200) — один и тот же контракт «не
# больше 200 строк за запрос» на всех списочных эндпоинтах.
MAX_LIMIT = 200

# Поиск по лицу возвращает не строки таблицы, а совпадения по вектору;
# 500 — практический потолок для одного запроса на целевом железе.
MAX_SEARCH_LIMIT = 500


def days_param(default: int) -> Query:
    """Окно выборки в днях: 1..MAX_DAYS."""
    return Query(default, ge=1, le=MAX_DAYS, description=f"Окно в днях (1..{MAX_DAYS})")


def limit_param(default: int, maximum: int = MAX_LIMIT) -> Query:
    """Размер выборки: 1..maximum."""
    return Query(default, ge=1, le=maximum, description=f"Сколько записей вернуть (1..{maximum})")


def limit_form(default: int, maximum: int = MAX_SEARCH_LIMIT) -> Form:
    """То же для multipart-формы (поиск по фото идёт multipart, не query)."""
    return Form(default, ge=1, le=maximum)


def threshold_form(default: float) -> Form:
    """Порог косинусной схожести: 0.0..1.0.

    Отрицательный порог превращал условие `similarity >= :threshold` в
    тавтологию, то есть выдавал всю таблицу событий вместо совпадений.
    """
    return Form(default, ge=0.0, le=1.0)
