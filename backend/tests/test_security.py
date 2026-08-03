"""Юнит-тесты безопасности: шифрование RTSP, хеш паролей, JWT.
Не требуют БД — проверяют чистую логику."""
import os

# Дефолтный ключ из config — валидный Fernet (32 байта в base64).
from app.services.encryption import encrypt, decrypt
from app.auth import hash_password, verify_password, create_token
from app.config import settings
from jose import jwt


def test_rtsp_encryption_roundtrip():
    url = "rtsp://admin:secret@192.168.1.10:554/Streaming/Channels/101"
    enc = encrypt(url)
    assert enc != url, "Зашифрованное значение не должно совпадать с исходным"
    assert "secret" not in enc, "Учётные данные не должны храниться в открытом виде"
    assert decrypt(enc) == url, "Расшифровка должна вернуть исходный URL"


def test_encryption_is_non_deterministic():
    url = "rtsp://camera/stream"
    assert encrypt(url) != encrypt(url), "Fernet добавляет IV — токены должны отличаться"


def test_password_hash_and_verify():
    h = hash_password("СложныйПароль123")
    assert h != "СложныйПароль123"
    assert h.startswith("$2"), "Должен использоваться bcrypt"
    assert verify_password("СложныйПароль123", h)
    assert not verify_password("неверный", h)


def test_jwt_contains_role_and_subject():
    token = create_token("operator1", "operator")
    payload = jwt.decode(token, settings.SECRET_KEY, algorithms=["HS256"])
    assert payload["sub"] == "operator1"
    assert payload["role"] == "operator"
    assert "exp" in payload


def test_cors_does_not_allow_wildcard_with_credentials():
    """allow_origins=['*'] + allow_credentials=True — небезопасная комбинация
    (и запрещена спецификацией fetch: браузер такой ответ отклонит).
    CORS должен ограничиваться конкретным списком источников."""
    from app.main import app
    from starlette.middleware.cors import CORSMiddleware

    cors = next(m for m in app.user_middleware if m.cls is CORSMiddleware)
    origins = cors.kwargs.get("allow_origins")
    assert origins != ["*"], "Список источников не должен быть '*'"
    assert cors.kwargs.get("allow_credentials") is not True or origins != ["*"]


def test_jwt_rejects_tampered_signature():
    token = create_token("admin", "admin")
    from jose import JWTError
    bad = token[:-3] + ("aaa" if not token.endswith("aaa") else "bbb")
    try:
        jwt.decode(bad, settings.SECRET_KEY, algorithms=["HS256"])
        raised = False
    except JWTError:
        raised = True
    assert raised, "Изменённый токен должен отвергаться"
