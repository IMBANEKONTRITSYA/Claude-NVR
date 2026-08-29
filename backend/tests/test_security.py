"""Юнит-тесты безопасности: шифрование RTSP, хеш паролей, JWT.
Не требуют БД — проверяют чистую логику."""
import os

# Дефолтный ключ из config — валидный Fernet (32 байта в base64).
from app.services.encryption import encrypt, decrypt
from app.auth import hash_password, verify_password, create_token
from app.config import settings, Settings, insecure_secret_problems
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


def test_camera_rejects_non_rtsp_scheme():
    """ffprobe/ffmpeg понимают множество протоколов (file:, http:, concat:,
    subprocess: и т.д.) — без ограничения на rtsp(s) поле стало бы SSRF/LFI-
    вектором через /api/cameras и /api/cameras/test."""
    from pydantic import ValidationError
    from app.schemas import CameraIn, RtspTest

    for bad_url in (
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "concat:/etc/passwd|/etc/shadow",
        "subprocess:id",
        "not-a-url",
        "",
    ):
        try:
            CameraIn(name="cam", rtsp_url=bad_url)
            raised = False
        except ValidationError:
            raised = True
        assert raised, f"CameraIn должен отклонять не-RTSP URL: {bad_url!r}"

        try:
            RtspTest(rtsp_url=bad_url)
            raised = False
        except ValidationError:
            raised = True
        assert raised, f"RtspTest должен отклонять не-RTSP URL: {bad_url!r}"


def test_camera_accepts_rtsp_and_rtsps():
    from app.schemas import CameraIn, RtspTest

    for url in ("rtsp://192.168.1.10:554/stream1", "rtsps://cam.local/ch0"):
        assert CameraIn(name="cam", rtsp_url=url).rtsp_url == url
        assert RtspTest(rtsp_url=url).rtsp_url == url


def test_camera_sub_rtsp_url_optional_but_validated():
    from pydantic import ValidationError
    from app.schemas import CameraIn

    cam = CameraIn(name="cam", rtsp_url="rtsp://cam/main", sub_rtsp_url=None)
    assert cam.sub_rtsp_url is None

    try:
        CameraIn(name="cam", rtsp_url="rtsp://cam/main", sub_rtsp_url="http://evil/sub")
        raised = False
    except ValidationError:
        raised = True
    assert raised, "sub_rtsp_url тоже должен ограничиваться rtsp(s)"


def test_default_secret_key_flagged_as_insecure():
    """SECRET_KEY, оставшийся из .env.example/docker-compose.yml, известен
    каждому, кто читал публичный репозиторий — приложение обязано считать
    его небезопасным, а не запускаться молча."""
    s = Settings(SECRET_KEY="change-me-in-production", RTSP_ENCRYPTION_KEY="my-own-random-key")
    problems = insecure_secret_problems(s)
    assert any("SECRET_KEY" in p for p in problems)


def test_default_rtsp_key_flagged_as_insecure():
    s = Settings(SECRET_KEY="my-own-random-key", RTSP_ENCRYPTION_KEY="ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=")
    problems = insecure_secret_problems(s)
    assert any("RTSP_ENCRYPTION_KEY" in p for p in problems)


def test_custom_secrets_pass_validation():
    s = Settings(
        SECRET_KEY="a-real-random-secret-32bytes+",
        RTSP_ENCRYPTION_KEY="another-real-random-key",
        ADMIN_PASSWORD="Xk9#mQ2vLp7$rT4w",
    )
    assert insecure_secret_problems(s) == []


def test_default_admin_password_flagged_as_insecure():
    """ADMIN_PASSWORD, оставшийся значением 'admin' из .env.example/
    docker-compose.yml, известен каждому — учётка администратора не должна
    молча оставаться доступной с публичным дефолтным паролем (ТЗ 13)."""
    s = Settings(
        SECRET_KEY="a-real-random-secret-32bytes+",
        RTSP_ENCRYPTION_KEY="another-real-random-key",
        ADMIN_PASSWORD="admin",
    )
    problems = insecure_secret_problems(s)
    assert any("ADMIN_PASSWORD" in p for p in problems)
