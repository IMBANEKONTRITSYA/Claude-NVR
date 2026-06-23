from cryptography.fernet import Fernet
from ..config import settings


def _fernet() -> Fernet:
    key = settings.RTSP_ENCRYPTION_KEY.encode()
    return Fernet(key)


def encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
