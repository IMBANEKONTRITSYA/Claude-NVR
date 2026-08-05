"""Аудит действий: запись действий пользователей в audit_log (ТЗ 13, ТЗ 15).

Ключ карты действий — `(HTTP-метод, шаблон пути роута)`, где шаблон берётся
из `request.scope["route"].path`, то есть ровно та строка, что стоит в
декораторе `@router.<method>(...)` (`/api/persons/{pid}/merge/{dst_id}`).
Раньше ключом был префикс пути и сопоставление шло с запасными вариантами
(«путь начинается с ключа», «путь содержит обе части ключа»). Такой матч по
префиксу молча присваивал новым эндпоинтам ярлык более общего: все четыре
`POST /api/cameras/onvif/*` (в том числе диагностический дамп и выдача
RTSP-адреса с учётными данными) писались в журнал как «Добавлена камера».
Точный матч по шаблону такой ошибки допустить не может — совпадения не
бывает «частичного», а неизвестный роут просто не попадает в журнал, что
ловится тестом `test_audit_route_coverage.py`.
"""
import logging

from jose import jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .config import settings
from .db import SessionLocal
from .models import AuditLog

# Дочерний logger «facewatch.backend» (настраивается в main.py:
# configure_logging) — пишет тем же JSON-форматтером через обработчик
# родителя, без второй настройки хендлеров здесь.
logger = logging.getLogger("facewatch.backend.audit")

# Человекочитаемые описания действий. Ключ — (метод, шаблон пути роута).
ACTIONS: dict[tuple[str, str], str] = {
    # --- аутентификация ---
    ("POST", "/api/auth/login"): "Вход в систему",
    ("POST", "/api/auth/logout"): "Выход из системы",
    ("POST", "/api/auth/change-password"): "Смена пароля",
    # --- камеры ---
    ("POST", "/api/cameras"): "Добавлена камера",
    ("PUT", "/api/cameras/{cam_id}"): "Изменена камера",
    ("DELETE", "/api/cameras/{cam_id}"): "Удалена камера",
    ("PATCH", "/api/cameras/{cam_id}/enabled"): "Камера включена/отключена",
    ("PUT", "/api/cameras/{cam_id}/roi"): "Изменены зоны детекции",
    ("POST", "/api/cameras/test"): "Тест RTSP-подключения",
    # ONVIF-эндпоинты раньше все четыре писались как «Добавлена камера»
    # (префиксный матч по ("POST", "/api/cameras")). Два из них выдают
    # чувствительные данные: stream-uri возвращает RTSP-адрес с
    # подставленными учётными данными камеры, describe — сырые SOAP-ответы.
    ("POST", "/api/cameras/onvif/profiles"): "Запрошены ONVIF-профили камеры",
    ("POST", "/api/cameras/onvif/stream-uri"): "Получен RTSP-адрес камеры по ONVIF",
    ("POST", "/api/cameras/onvif/describe"): "Диагностический дамп ONVIF-ответов камеры",
    ("POST", "/api/cameras/onvif/bulk-add"): "Массовое добавление камер (ONVIF)",
    # --- пользователи ---
    ("POST", "/api/users"): "Создан пользователь",
    ("DELETE", "/api/users/{user_id}"): "Удалён пользователь",
    # --- персоны ---
    ("POST", "/api/persons"): "Создана персона (вручную)",
    ("PATCH", "/api/persons/{pid}"): "Изменена персона",
    ("DELETE", "/api/persons/{pid}"): "Удалена персона",
    ("POST", "/api/persons/{src_id}/merge/{dst_id}"): "Слияние персон",
    ("POST", "/api/persons/{pid}/enhance"): "Ручной апскейл персоны",
    # --- настройки ---
    ("PUT", "/api/settings"): "Изменены системные настройки",
    # «Смена профиля производительности» — отдельная строка матрицы прав
    # (SPEC.md, только admin), а записи в журнале не было вовсе: ключа
    # ("POST", "/api/settings/...") в карте не существовало, а ("PUT",
    # "/api/settings") не подходил по методу.
    ("POST", "/api/settings/profile/{name}"): "Смена профиля производительности",
    ("POST", "/api/settings/test-telegram"): "Проверка Telegram-уведомлений",
    # --- поиск ---
    ("POST", "/api/search/face"): "Поиск по фото",
    # --- выгрузки (ТЗ 13: «операции экспорта фиксируются в журнале аудита») ---
    # Все восемь — GET, поэтому до этого не аудировались вообще: middleware
    # выходила сразу для всех методов, кроме POST/PUT/PATCH/DELETE. Именно
    # эти операции выносят данные за пределы системы (весь журнал появлений
    # или сам журнал аудита в файл), то есть их отсутствие в журнале — самый
    # заметный пробел следа для разбора инцидента.
    ("GET", "/api/reports/appearances.csv"): "Экспорт отчёта: появления (CSV)",
    ("GET", "/api/reports/appearances.xlsx"): "Экспорт отчёта: появления (XLSX)",
    ("GET", "/api/reports/persons.csv"): "Экспорт отчёта: персоны (CSV)",
    ("GET", "/api/reports/persons.xlsx"): "Экспорт отчёта: персоны (XLSX)",
    ("GET", "/api/reports/cameras.csv"): "Экспорт отчёта: камеры (CSV)",
    ("GET", "/api/reports/cameras.xlsx"): "Экспорт отчёта: камеры (XLSX)",
    ("GET", "/api/audit/export.csv"): "Экспорт журнала аудита (CSV)",
    ("GET", "/api/audit/export.xlsx"): "Экспорт журнала аудита (XLSX)",
}

# Мутирующие роуты, для которых запись сознательно не делается. Пустой
# ярлык — не «забыли», а решение: тест покрытия роутов требует, чтобы
# каждый мутирующий роут был либо в ACTIONS, либо здесь с причиной, иначе
# новый эндпоинт снова окажется вне журнала незамеченным.
NOT_AUDITED: dict[tuple[str, str], str] = {
    ("POST", "/api/auth/refresh"): (
        "Автоматическое продление сессии браузером, не действие оператора: "
        "запись шла бы раз в ACCESS_TOKEN_EXPIRE_MINUTES на каждую открытую "
        "вкладку и вытесняла бы из журнала реальные действия. Кража "
        "refresh-токена ловится не журналом, а ротацией с отзывом семьи "
        "токенов (auth.py: rotate_refresh_token)."
    ),
}


def _action_for(method: str, route_path: str | None) -> str | None:
    """Ярлык журнала для роута или None, если роут аудировать не нужно.

    `route_path` — шаблон из `scope["route"].path`, а не фактический URL:
    у несопоставленных запросов (404) роута нет, и они в журнал не идут.
    """
    if route_path is None:
        return None
    return ACTIONS.get((method, route_path))


def _extract_user(request: Request) -> tuple[str, str] | None:
    auth = request.headers.get("Authorization") or ""
    token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else request.query_params.get("token")
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    except JWTError:
        return None
    return payload.get("sub") or "", payload.get("role") or ""


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Для логина читаем username из form-body до передачи запроса дальше
        login_user: str | None = None
        if request.method == "POST" and request.url.path == "/api/auth/login":
            try:
                body = await request.body()
                from urllib.parse import parse_qs
                form = parse_qs(body.decode("utf-8", errors="ignore"))
                login_user = (form.get("username") or [""])[0]
                # Возвращаем тело обратно, чтобы downstream его получил
                async def receive():
                    return {"type": "http.request", "body": body, "more_body": False}
                request = Request(request.scope, receive)
            except Exception:
                logger.warning("не удалось прочитать имя пользователя из тела /login", exc_info=True)

        response = await call_next(request)

        # scope["route"] проставляет Starlette при матчинге роута, то есть
        # он уже на месте к моменту возврата из call_next (scope — тот же
        # объект, что ушёл вниз по стеку).
        route = request.scope.get("route")
        action = _action_for(request.method, getattr(route, "path", None))
        if not action:
            return response

        if request.url.path == "/api/auth/login":
            username = login_user or "?"
            role = "?"
        else:
            user = _extract_user(request)
            if not user:
                return response
            username, role = user

        try:
            async with SessionLocal() as db:
                db.add(AuditLog(
                    username=username, role=role, action=action,
                    method=request.method, path=request.url.path,
                    status_code=response.status_code,
                    ip=(request.headers.get("x-real-ip")
                        or (request.client.host if request.client else None)),
                ))
                await db.commit()
        except Exception:
            # Потеря записи журнала не должна ронять сам запрос, но и
            # оставаться невидимой не должна: аудит — контроль из ТЗ 13, и
            # молча пропавшие записи невозможно заметить постфактум.
            logger.error("не удалось записать действие в журнал аудита",
                         extra={"audit_action": action, "path": request.url.path},
                         exc_info=True)
        return response
