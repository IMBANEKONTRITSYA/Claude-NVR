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


# --- Секреты в таблице settings ---------------------------------------------
# RTSP-учётки лежат в отдельных *_enc-колонках, поэтому «зашифровано или нет»
# там видно по схеме. Таблица settings — общий key/value для всех настроек, и
# секрет (telegram_bot_token) хранился в ней открытым текстом наравне с
# retention_days: в дампе pg_dump, который backup/run.sh кладёт в /backups и
# держит 14 дней, токен бота читался глазами. ТЗ 13 требует хранить секреты
# зашифрованными, поэтому значение помечается префиксом и шифруется тем же
# Fernet-ключом, что и RTSP.
#
# Префикс нужен именно для совместимости: в уже развёрнутых БД значение лежит
# открытым текстом, и отличить его от шифротекста больше нечем. Значение без
# префикса читается как legacy-plaintext (система продолжает работать), а
# первая же запись — из админки или из миграции при старте backend'а —
# переводит его в зашифрованный вид.
SECRET_SETTING_PREFIX = "enc:v1:"

# Ключи settings, значения которых должны храниться зашифрованными.
# smtp_password — тот же класс секрета, что и токен бота: пароль от почтового
# ящика объекта в открытом виде в дампе pg_dump (backup/run.sh держит их 14
# дней) даёт доступ к переписке, а часто и к учётке целиком.
SECRET_SETTING_KEYS = frozenset({"telegram_bot_token", "smtp_password"})


def encrypt_setting(plain: str) -> str:
    """Шифрует значение секретной настройки. Пустая строка (секрет не задан)
    остаётся пустой — шифровать нечего, а пустое значение отключает функцию."""
    if not plain:
        return ""
    return SECRET_SETTING_PREFIX + encrypt(plain)


def needs_secret_migration(key: str, value: str) -> bool:
    """True, если значение настройки — секрет, лежащий открытым текстом.

    Вынесено из main.py:lifespan отдельной функцией, чтобы решение
    «шифровать или не трогать» можно было проверить тестами без поднятия
    приложения: второй TestClient в том же прогоне создаёт свой event loop и
    ломает пул asyncpg, общий с сессионной фикстурой (см. tests/conftest.py).
    """
    return key in SECRET_SETTING_KEYS and bool(value) and not value.startswith(SECRET_SETTING_PREFIX)


def decrypt_setting(stored: str) -> str:
    """Возвращает открытое значение секретной настройки.

    Значение без префикса — legacy-plaintext из БД, развёрнутой до этого
    фикса: возвращаем как есть, иначе оповещения молча перестали бы работать
    при обновлении. Нерасшифровываемое значение с префиксом (например, БД
    восстановлена из бэкапа, а RTSP_ENCRYPTION_KEY в .env сменился) — это не
    plaintext-токен, отдавать его наружу нельзя, поэтому пустая строка:
    оповещения отключатся, но мусор не уедет в Telegram API.
    """
    if not stored or not stored.startswith(SECRET_SETTING_PREFIX):
        return stored or ""
    try:
        return decrypt(stored[len(SECRET_SETTING_PREFIX):])
    except Exception:
        return ""
