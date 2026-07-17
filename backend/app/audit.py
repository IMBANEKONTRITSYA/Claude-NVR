"""Аудит действий: записывает мутирующие запросы (POST/PUT/PATCH/DELETE) в audit_log."""
from jose import jwt, JWTError
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from .config import settings
from .db import SessionLocal
from .models import AuditLog

# Человекочитаемые описания путей (path может содержать {id})
ACTIONS = {
    ("POST", "/api/auth/login"): "Вход в систему",
    ("POST", "/api/auth/change-password"): "Смена пароля",
    ("POST", "/api/cameras"): "Добавлена камера",
    ("PUT", "/api/cameras/"): "Изменена камера",
    ("DELETE", "/api/cameras/"): "Удалена камера",
    ("PATCH", "/api/cameras/", "/enabled"): "Камера включена/отключена",
    ("PUT", "/api/cameras/", "/roi"): "Изменены зоны детекции",
    ("POST", "/api/cameras/test"): "Тест RTSP-подключения",
    ("POST", "/api/users"): "Создан пользователь",
    ("DELETE", "/api/users/"): "Удалён пользователь",
    ("POST", "/api/persons"): "Создана персона (вручную)",
    ("PATCH", "/api/persons/"): "Изменена персона",
    ("DELETE", "/api/persons/"): "Удалена персона",
    ("POST", "/api/persons/", "/merge/"): "Слияние персон",
    ("POST", "/api/persons/", "/enhance"): "Ручной апскейл персоны",
    ("PUT", "/api/settings"): "Изменены системные настройки",
    ("POST", "/api/search/face"): "Поиск по фото",
}


def _action_for(method: str, path: str) -> str | None:
    if (method, path) in ACTIONS:
        return ACTIONS[(method, path)]
    # Сначала специфичные шаблоны (3-туплы), иначе /api/persons/5/merge/3
    # матчился бы префиксом ("POST", "/api/persons") как "Создана персона".
    for key, label in ACTIONS.items():
        if len(key) == 3 and key[0] == method and key[1] in path and key[2] in path:
            return label
    for key, label in ACTIONS.items():
        if len(key) == 2 and key[0] == method and path.startswith(key[1]):
            return label
    return None


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
                pass

        response = await call_next(request)
        if request.method not in ("POST", "PUT", "PATCH", "DELETE"):
            return response

        action = _action_for(request.method, request.url.path)
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
                    ip=(request.client.host if request.client else None),
                ))
                await db.commit()
        except Exception:
            pass
        return response
