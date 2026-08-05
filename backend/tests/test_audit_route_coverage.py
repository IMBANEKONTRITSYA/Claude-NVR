"""Журнал аудита (ТЗ 13) не должен молча расходиться с таблицей роутов.

Находка цикла 20: карта действий `app/audit.py:ACTIONS` поддерживалась
руками и сопоставлялась с запросом по префиксу пути, из-за чего разошлась с
фактическим набором эндпоинтов:

* все восемь операций экспорта (`/api/reports/*.csv|xlsx`,
  `/api/audit/export.*`) не писались в журнал вообще — ТЗ 13 требует
  обратного («операции экспорта фиксируются в журнале аудита»), а middleware
  выходила сразу для любого метода, кроме POST/PUT/PATCH/DELETE;
* `POST /api/settings/profile/{name}` (отдельная строка матрицы прав «Смена
  профиля производительности») не писался в журнал: ключа с этим путём в
  карте не было, а ("PUT", "/api/settings") не подходил по методу;
* четыре `POST /api/cameras/onvif/*` писались как «Добавлена камера»,
  потому что их путь начинается с ключа ("POST", "/api/cameras") — в том
  числе выдача RTSP-адреса с учётными данными и дамп SOAP-ответов.

Тесты ниже — не проверка конкретных ярлыков, а инвариант: каждый роут,
который меняет состояние или выносит данные из системы, должен иметь
осознанное решение — либо запись в журнал (`ACTIONS`), либо явную причину
не писать (`NOT_AUDITED`). Новый эндпоинт без такого решения роняет CI, то
есть повторение находки цикла 20 становится невозможным незаметно.

Статические (без БД) — читают таблицу роутов приложения; production path
на настоящем Postgres проверяется в `test_integration_routes.py`
(`test_audit_log_records_export_operations`).
"""
from fastapi.routing import iter_route_contexts

from app.audit import ACTIONS, NOT_AUDITED, _action_for
from app.main import app

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
# Выгрузки — GET, но именно они выносят данные за пределы системы (ТЗ 13).
EXPORT_SUFFIXES = (".csv", ".xlsx")


def _app_routes() -> set[tuple[str, str]]:
    """Все (метод, шаблон пути) приложения; HEAD/OPTIONS отбрасываются.

    `iter_route_contexts` разворачивает вложенные `include_router` в плоский
    список с полными путями (с префиксом роутера). Обходить `app.routes`
    напрямую нельзя: с FastAPI 0.141 подключённые роутеры лежат там
    единственным объектом-обёрткой на роутер, а не своими APIRoute, поэтому
    наивный фильтр `isinstance(route, APIRoute)` находит только два роута,
    объявленных прямо на `app`, и проверки ниже проходили бы впустую (ровно
    это и произошло при первом прогоне — см. `test_route_table_is_not_empty`).

    Шаблон совпадает со `scope["route"].path`, который читает middleware:
    проверено на живом приложении (`/api/settings/profile/{name}`, а не
    `/api/settings/profile/economy`).
    """
    out: set[tuple[str, str]] = set()
    for ctx in iter_route_contexts(app.routes):
        for method in ctx.methods or ():
            if method in ("HEAD", "OPTIONS"):
                continue
            out.add((method, ctx.path))
    return out


def test_route_table_is_not_empty():
    """Страховка от «зелёных впустую» проверок ниже: они устроены как «нет
    роутов без решения», и на пустом перечислении прошли бы при полностью
    сломанной карте аудита."""
    routes = _app_routes()
    assert len(routes) > 30, f"перечисление роутов сломано, найдено: {sorted(routes)}"
    assert ("POST", "/api/cameras") in routes
    assert ("GET", "/api/audit/export.xlsx") in routes


def test_every_mutating_route_has_an_audit_decision():
    undecided = sorted(
        (m, p) for m, p in _app_routes()
        if m in MUTATING and (m, p) not in ACTIONS and (m, p) not in NOT_AUDITED
    )
    assert not undecided, (
        "мутирующие роуты без решения по аудиту (добавьте в ACTIONS либо в "
        f"NOT_AUDITED с причиной): {undecided}"
    )


def test_every_export_route_is_audited():
    """ТЗ 13: «операции экспорта фиксируются в журнале аудита»."""
    missing = sorted(
        (m, p) for m, p in _app_routes()
        if p.endswith(EXPORT_SUFFIXES) and (m, p) not in ACTIONS
    )
    assert not missing, f"операции экспорта вне журнала аудита: {missing}"


def test_audit_map_has_no_entries_for_routes_that_no_longer_exist():
    """Обратная сторона: переименованный роут не должен оставлять в карте
    мёртвый ключ, который выглядит как покрытие, но никогда не сработает."""
    routes = _app_routes()
    stale = sorted(k for k in list(ACTIONS) + list(NOT_AUDITED) if k not in routes)
    assert not stale, f"ключи карты аудита без соответствующего роута: {stale}"


def test_not_audited_entries_carry_a_reason():
    empty = sorted(k for k, reason in NOT_AUDITED.items() if not (reason or "").strip())
    assert not empty, f"NOT_AUDITED без причины: {empty}"


def test_onvif_endpoints_are_not_labelled_as_camera_creation():
    """Регрессия матча по префиксу: до фикса все четыре возвращали
    «Добавлена камера» — ярлык совсем другого действия."""
    for path in (
        "/api/cameras/onvif/profiles",
        "/api/cameras/onvif/stream-uri",
        "/api/cameras/onvif/describe",
        "/api/cameras/onvif/bulk-add",
    ):
        label = _action_for("POST", path)
        assert label, f"{path} не аудируется"
        assert label != ACTIONS[("POST", "/api/cameras")], (
            f"{path} пишется в журнал как добавление камеры"
        )


def test_unmatched_request_is_not_audited():
    """404 и запросы вне таблицы роутов не должны создавать записей: роута
    нет — значит и шаблона для матча нет."""
    assert _action_for("POST", None) is None
    assert _action_for("POST", "/api/does-not-exist") is None


def test_action_lookup_is_exact_not_prefix():
    """Ключ карты — шаблон роута целиком. Путь, который лишь начинается с
    известного шаблона, ярлык получать не должен."""
    assert _action_for("POST", "/api/cameras") == "Добавлена камера"
    assert _action_for("POST", "/api/cameras/something-new") is None
    assert _action_for("PUT", "/api/settings") == "Изменены системные настройки"
    assert _action_for("PUT", "/api/settings/something-new") is None
