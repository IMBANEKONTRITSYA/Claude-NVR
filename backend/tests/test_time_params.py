"""Ни один эндпоинт не принимает время без нормализации к naive-UTC.

Сторож к `app/timeparams.py`. Дефект D11 (DEPLOY_FINDINGS_2026-10-28) жил
потому, что нормализацию делал каждый обработчик сам: экспорт архива её
делал, соседние эндпоинты — нет. Проверка идёт по собранному приложению,
а не по тексту исходников: так она видит и Query, и Form, и параметры,
объявленные через Depends, — то, что FastAPI на самом деле разбирает.
"""
from datetime import datetime, timedelta, timezone
from typing import get_args

from fastapi.routing import APIRoute

from app.main import app


def _is_datetime(annotation) -> bool:
    if annotation is datetime:
        return True
    return any(_is_datetime(a) for a in get_args(annotation))


_AWARE = datetime(2026, 10, 28, 20, 0, tzinfo=timezone(timedelta(hours=5)))
_EXPECTED = datetime(2026, 10, 28, 15, 0)


def _is_normalized(field) -> bool:
    """Поведенческая проверка: параметр, получив время с поясом через
    собственную валидацию FastAPI, обязан отдать naive-UTC.

    Не интроспекция аннотаций, и это существенно: при `NaiveUtc =
    Query(...)` аннотация выглядит правильной, а валидатор FastAPI теряет.
    Интроспекция была бы зелёной, запрос — 500-м.
    """
    value, errors = field.validate(_AWARE, {}, loc=("query", field.name))
    return not errors and value == _EXPECTED and value.tzinfo is None


def _api_routes(routes):
    """Все APIRoute приложения, включая вложенные роутеры.

    FastAPI 0.14x кладёт `include_router` в `app.routes` ленивой обёрткой
    `_IncludedRouter`, а не развёрнутыми маршрутами: плоский обход
    `app.routes` видит только /docs и /openapi.json, и сторож был бы зелёным
    при любом коде (это и поймал второй тест ниже при написании).
    """
    for route in routes:
        if isinstance(route, APIRoute):
            yield route
        inner = getattr(route, "original_router", None) or getattr(route, "router", None)
        if inner is not None and inner is not route and hasattr(inner, "routes"):
            yield from _api_routes(inner.routes)


def _all_params(dependant):
    yield from dependant.query_params
    yield from dependant.body_params
    for sub in dependant.dependencies:
        yield from _all_params(sub)


def test_every_datetime_request_param_is_normalized():
    offenders = []
    for route in _api_routes(app.routes):
        for field in _all_params(route.dependant):
            if _is_datetime(field.field_info.annotation) and not _is_normalized(field):
                offenders.append(f"{sorted(route.methods)} {route.path} :: {field.name}")
    assert not offenders, (
        "время во входных параметрах без нормализации к naive-UTC — "
        "aware-значение уронит запрос 500-й; объявите параметр как "
        "app.timeparams.NaiveUtc:\n  " + "\n  ".join(offenders)
    )


def test_guard_sees_the_known_endpoints():
    """Сторож не пуст: те эндпоинты, где D11 воспроизводился, он видит.

    Без этой проверки поломка `_all_params` (скажем, переименование
    атрибутов FastAPI) сделала бы первый тест зелёным при любом коде.
    """
    seen = set()
    for route in _api_routes(app.routes):
        for field in _all_params(route.dependant):
            if _is_datetime(field.field_info.annotation):
                seen.add(route.path)
    for path in ("/api/search/face", "/api/audit", "/api/archive/segments", "/api/archive/export"):
        assert path in seen, f"сторож не видит параметров времени у {path}"
