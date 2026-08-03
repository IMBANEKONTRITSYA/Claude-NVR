"""Smoke-тест: приложение импортируется со всеми роутерами и ключевые маршруты на месте."""
from app.main import app


def _paths():
    # Начиная с FastAPI/Starlette 1.x include_router() оборачивает вложенные
    # маршруты в промежуточный объект (Mount/_IncludedRouter) вместо плоского
    # списка APIRoute — обходим рекурсивно, чтобы тест не зависел от версии.
    paths = set()

    def walk(routes):
        for r in routes:
            p = getattr(r, "path", None)
            if p:
                paths.add(p)
            router = getattr(r, "original_router", None)
            sub = getattr(router, "routes", None) or getattr(r, "routes", None)
            if sub:
                walk(sub)

    walk(app.routes)
    return paths


def test_core_routes_present():
    paths = _paths()
    for p in [
        "/api/auth/login",
        "/api/cameras",
        "/api/persons",
        "/api/search/face",
        "/api/persons/{pid}/enhance",
        "/api/reports/persons.csv",
        "/api/reports/cameras.csv",
        "/api/settings",
        "/api/settings/test-telegram",
        "/api/audit",
        "/api/audit/export.csv",
        "/api/cameras/{cam_id}/enabled",
        "/ws/faces",
    ]:
        assert p in paths, f"Отсутствует маршрут {p}"
