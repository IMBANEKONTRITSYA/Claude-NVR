import base64
import hashlib
from cryptography.fernet import Fernet
from ..config import settings


def normalize_fernet_key(raw: str) -> bytes:
    """Возвращает валидный Fernet-ключ из любой строки.
    Если raw уже корректный Fernet-ключ (32 байта в base64url) — используем как есть,
    иначе детерминированно выводим из SHA-256(raw). Backend и worker должны
    использовать одинаковую логику, иначе расшифровка не сойдётся."""
    raw_bytes = raw.encode()
    try:
        Fernet(raw_bytes)  # проверка валидности
        return raw_bytes
    except Exception:
        return base64.urlsafe_b64encode(hashlib.sha256(raw_bytes).digest())


def _fernet() -> Fernet:
    return Fernet(normalize_fernet_key(settings.RTSP_ENCRYPTION_KEY))


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
