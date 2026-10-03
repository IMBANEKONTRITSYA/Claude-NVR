"""Секреты в таблице settings должны храниться зашифрованными (ТЗ 13).

Находка цикла 18: `telegram_bot_token` лежал в key/value-таблице `settings`
открытым текстом — наравне с `retention_days`. RTSP-учётки шифруются
Fernet'ом в отдельных `*_enc`-колонках именно потому, что попадают в дампы;
токен бота той же защиты не имел и читался глазами в `pg_dump`, который
`backup/run.sh` кладёт в `/backups` и хранит 14 дней (ТЗ 12 требует эти
бэкапы на отдельном диске/сетевом хранилище — то есть за пределами хоста).

Тесты не требуют БД: проверяется чистая логика шифрования настроек и её
зеркало в воркере.
"""
import pytest

from app.services.encryption import (
    SECRET_SETTING_KEYS,
    SECRET_SETTING_PREFIX,
    decrypt,
    decrypt_setting,
    encrypt_setting,
)

TOKEN = "7123456789:AAHrandom-bot-token-value_XYZ"


def test_telegram_token_is_a_secret_key():
    """Набор секретных ключей должен включать токен бота — иначе весь
    механизм ниже к нему просто не применится."""
    assert "telegram_bot_token" in SECRET_SETTING_KEYS


def test_encrypted_value_does_not_contain_plaintext():
    stored = encrypt_setting(TOKEN)
    assert stored != TOKEN
    assert TOKEN not in stored
    # Отдельно — «хвост» токена: даже частичная утечка в дампе недопустима.
    assert "AAHrandom-bot-token-value_XYZ" not in stored


def test_roundtrip():
    assert decrypt_setting(encrypt_setting(TOKEN)) == TOKEN


def test_encryption_is_non_deterministic():
    """Fernet добавляет IV — два сохранения одного токена дают разные
    шифротексты, по дампу нельзя сопоставить значения между инсталляциями."""
    assert encrypt_setting(TOKEN) != encrypt_setting(TOKEN)


def test_stored_value_is_marked_and_decryptable_by_shared_key():
    """Префикс — то, чем legacy-plaintext отличается от шифротекста;
    под ним должен лежать обычный Fernet-токен на общем ключе (иначе воркер,
    у которого своя копия логики, не расшифрует)."""
    stored = encrypt_setting(TOKEN)
    assert stored.startswith(SECRET_SETTING_PREFIX)
    assert decrypt(stored[len(SECRET_SETTING_PREFIX):]) == TOKEN


def test_empty_secret_stays_empty():
    """Пустое значение отключает оповещения — шифровать нечего, и пустота
    должна остаться пустотой в обе стороны."""
    assert encrypt_setting("") == ""
    assert decrypt_setting("") == ""


def test_legacy_plaintext_is_read_as_is():
    """БД, развёрнутая до фикса, хранит токен открытым текстом. Читаться он
    обязан по-прежнему, иначе обновление молча ломает оповещения."""
    assert decrypt_setting(TOKEN) == TOKEN


def test_undecryptable_value_yields_empty_not_garbage():
    """БД восстановлена из бэкапа, а RTSP_ENCRYPTION_KEY в .env сменился:
    значение с префиксом не расшифровывается. Это не plaintext-токен —
    наружу должна уйти пустая строка (оповещения отключатся), а не мусор."""
    assert decrypt_setting(SECRET_SETTING_PREFIX + "не-шифротекст") == ""


def test_worker_mirror_reads_backend_ciphertext(monkeypatch):
    """Воркер держит собственную копию логики (он не импортирует backend).
    Копия обязана расшифровывать то, что записал backend — иначе оповещения
    отвалятся ровно в проде, где сервисы разные, и ни один тест одной
    стороны этого не покажет."""
    import base64
    import hashlib

    from cryptography.fernet import Fernet

    from app.config import settings

    # Воспроизводим ключ ровно так же, как worker.py (_normalize_fernet_key).
    raw = settings.RTSP_ENCRYPTION_KEY.encode()
    try:
        Fernet(raw)
        key = raw
    except Exception:
        key = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
    worker_fernet = Fernet(key)

    def worker_decrypt_setting(stored: str) -> str:
        if not stored or not stored.startswith("enc:v1:"):
            return stored or ""
        return worker_fernet.decrypt(stored[len("enc:v1:"):].encode()).decode()

    assert worker_decrypt_setting(encrypt_setting(TOKEN)) == TOKEN
    assert worker_decrypt_setting(TOKEN) == TOKEN, "legacy-plaintext и в воркере"


def test_settings_api_stores_ciphertext_and_returns_plaintext(client, admin_headers, pg_conn):
    """Сквозная проверка на настоящем Postgres: то, что реально легло в
    таблицу `settings` (а значит — и в дамп pg_dump), не должно содержать
    токен, при этом админка обязана получать его обратно в открытом виде —
    иначе форма настроек затрёт токен при первом же сохранении.

    Юнит-тесты выше проверяют функции шифрования; здесь проверяется, что
    роутер их действительно вызывает на обоих концах.
    """
    r = client.put("/api/settings", json={"telegram_bot_token": TOKEN}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["telegram_bot_token"] == TOKEN, "ответ PUT — открытый токен для формы"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'telegram_bot_token'")
        stored = cur.fetchone()[0]
    assert stored.startswith(SECRET_SETTING_PREFIX), "в БД должен лежать шифротекст"
    assert TOKEN not in stored
    assert decrypt_setting(stored) == TOKEN

    r = client.get("/api/settings", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["telegram_bot_token"] == TOKEN, "GET — открытый токен для формы"

    # Возвращаем настройку в исходное состояние, чтобы не влиять на соседние
    # тесты, использующие ту же сессионную БД.
    client.put("/api/settings", json={"telegram_bot_token": ""}, headers=admin_headers)


@pytest.mark.parametrize(
    "key, value, expected",
    [
        ("telegram_bot_token", TOKEN, True),                       # legacy-plaintext — мигрируем
        ("telegram_bot_token", SECRET_SETTING_PREFIX + "x", False),  # уже зашифровано
        ("telegram_bot_token", "", False),                         # секрет не задан
        ("retention_days", "30", False),                           # обычная настройка
    ],
)
def test_startup_migration_targets_only_legacy_plaintext_secrets(key, value, expected):
    """Миграция из main.py:lifespan дошифровывает секреты в уже развёрнутых
    БД — без неё фикс подействовал бы только после того, как администратор
    вручную пересохранит форму настроек.

    Проверяется само решение «шифровать или не трогать»: оно должно быть
    идемпотентным (повторный старт не перешифровывает) и не задевать обычные
    настройки. Поднять lifespan второй раз в том же прогоне нельзя — второй
    TestClient создаёт свой event loop и ломает пул asyncpg, общий с
    сессионной фикстурой (см. tests/conftest.py), поэтому предикат и вынесен
    отдельной функцией.
    """
    from app.services.encryption import needs_secret_migration

    assert needs_secret_migration(key, value) is expected


def test_startup_migration_is_idempotent():
    """Результат миграции не должен требовать миграции снова."""
    from app.services.encryption import needs_secret_migration

    migrated = encrypt_setting(TOKEN)
    assert needs_secret_migration("telegram_bot_token", migrated) is False
    assert decrypt_setting(migrated) == TOKEN


def test_worker_constants_match_backend():
    """Префикс и набор ключей продублированы в worker/worker.py текстом.
    Если одну сторону поменяют, тест укажет на вторую."""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[2] / "worker" / "worker.py").read_text()
    prefix = re.search(r'^SECRET_SETTING_PREFIX = "([^"]+)"', src, re.M)
    assert prefix and prefix.group(1) == SECRET_SETTING_PREFIX
    keys = re.search(r"^SECRET_SETTING_KEYS = frozenset\(\{([^}]*)\}\)", src, re.M)
    assert keys
    worker_keys = set(re.findall(r'"([^"]+)"', keys.group(1)))
    assert worker_keys == set(SECRET_SETTING_KEYS)
