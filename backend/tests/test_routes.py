"""Smoke-тест: приложение импортируется со всеми роутерами и ключевые маршруты на месте."""
from app.main import app


def _paths():
    return {getattr(r, "path", None) for r in app.routes}


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
