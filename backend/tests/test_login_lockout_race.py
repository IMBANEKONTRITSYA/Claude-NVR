"""Регрессионный тест на находку targeted P0 spot-check цикла 14 (см.
docs/reviews/REVIEW_LOG.md): PR #35 (цикл 13) перенёс bcrypt в threadpool
(asyncio.to_thread) в /api/auth/login и /api/auth/change-password, чтобы
конкурентные запросы не блокировали event loop друг для друга (см.
test_auth_async_bcrypt.py). Побочный эффект — до этого фикса синхронный
bcrypt внутри async-обработчика случайно сериализовал конкурентные запросы
на один и тот же ip:username, маскируя TOCTOU-гонку в лимите попыток:
"читаем счётчик из Redis → проверяем пароль → инкрементируем счётчик
только при неудаче" — пачка одновременных запросов могла прочитать один и
тот же счётчик "лимит ещё не исчерпан" до того, как хоть один из них успел
его увеличить, и пробить лимит `MAX_ATTEMPTS`/`CHANGE_PW_MAX_ATTEMPTS`
пачкой параллельных попыток.

Фикс (backend/app/routers/auth.py) резервирует попытку атомарным
`redis.incr()` ДО bcrypt-проверки вместо инкремента постфактум только при
неудаче — INCR в Redis атомарен независимо от того, сколько bcrypt-вызовов
выполняется параллельно в threadpool'е.

Этот файл проверяет обе стороны:
1. реальный эндпоинт `/api/auth/login`, вызванный по-настоящему конкурентно
   (несколько ОС-потоков одновременно бьют в общий `client` из conftest.py —
   TestClient маршрутизирует их через блокирующий портал в один и тот же
   event loop backend'а, так что реальный конкурентный overlap bcrypt-
   вызовов в threadpool'е воспроизводится, как и в проде под uvicorn), не
   пропускает больше `MAX_ATTEMPTS` попыток, даже под нагрузкой в
   несколько раз выше лимита;
2. негативный контроль (тот же методологический паттерн, что и в
   test_auth_async_bcrypt.py) — воспроизводит старый небезопасный паттерн
   "GET → проверка → INCR при неудаче" напрямую поверх настоящего Redis (на
   собственном изолированном соединении и event loop'е — не через общий
   get_redis()/engine backend'а, чтобы не пересекать loop'ы с сессионным
   TestClient'ом из conftest.py) и показывает, что ИМЕННО ОН пропускает
   больше лимита под конкурентной нагрузкой, доказывая, что тест №1
   чувствителен к самой гонке, а не проходит вне зависимости от того,
   атомарен фикс или нет.
"""
import threading
import uuid

import pytest

from app.routers.auth import MAX_ATTEMPTS


def test_concurrent_wrong_logins_never_exceed_lockout_limit(client):
    # Уникальный IP на тест — не делить redis-ключ login_fail:{ip}:{user}
    # с другими тестами файла/сессии, гоняющимися с тем же admin-логином.
    # Ключ сам истечёт по TTL (WINDOW_SEC=300с в auth.py) — отдельная чистка
    # не нужна, соединение к Redis тут используется только через `client`
    # (тот же event loop backend'а, что и у остальных интеграционных тестов).
    real_ip = f"race-test-{uuid.uuid4()}"
    n = MAX_ATTEMPTS * 3  # заведомо больше лимита — если гонка жива, лимит будет пробит

    statuses: list[int] = []
    lock = threading.Lock()

    def one():
        r = client.post(
            "/api/auth/login",
            data={"username": "admin", "password": "definitely-wrong-race-test"},
            headers={"X-Real-IP": real_ip},
        )
        with lock:
            statuses.append(r.status_code)

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    allowed = sum(1 for s in statuses if s == 401)  # реально дошли до bcrypt-проверки
    blocked = sum(1 for s in statuses if s == 429)
    assert allowed + blocked == n, f"неожиданные коды ответа: {statuses}"
    assert allowed <= MAX_ATTEMPTS, (
        f"{allowed} конкурентных попыток прошли bcrypt-проверку при лимите "
        f"{MAX_ATTEMPTS} — лимит попыток пробит гонкой (regression PR #35 / "
        f"targeted P0 spot-check цикла 14)"
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_control_get_then_increment_pattern_exceeds_limit_under_race():
    """Негативный контроль — на собственном изолированном redis-соединении
    (не через общий app.services.pubsub.get_redis(), чтобы не делить его
    event-loop-привязанное состояние с сессионным TestClient'ом из
    conftest.py), без FastAPI: воспроизводит старый паттерн auth.py до
    этого фикса ("прочитать счётчик → сымитировать медленную bcrypt-
    проверку → инкрементировать только при неудаче") и доказывает, что
    именно он, а не тест-методология, пропускает больше `MAX_ATTEMPTS`
    конкурентных попыток."""
    import asyncio

    import redis.asyncio as redis_asyncio

    from app.config import settings

    r = redis_asyncio.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await r.ping()
    except Exception as e:
        # Соседние тесты этого файла идут через фикстуру `client`, которая
        # аккуратно пропускается без Postgres/Redis (см. conftest.py). Этот
        # тест поднимает своё соединение и до этой правки падал жёстким
        # ConnectionError, давая ложное "1 failed" в локальном прогоне без
        # docker-compose. В CI Redis есть сервис-контейнером, так что там
        # тест как выполнялся, так и выполняется.
        await r.aclose()
        pytest.skip(f"Redis недоступен ({settings.REDIS_URL}): {e}")

    key = f"login_fail:control-{uuid.uuid4()}:admin"
    n = MAX_ATTEMPTS * 3

    async def old_racy_attempt():
        attempts = int(await r.get(key) or 0)
        if attempts >= MAX_ATTEMPTS:
            return 429
        # Имитирует конкурентный bcrypt в threadpool: реальная работа
        # (hash-подсчёт) занимает время, за которое другие корутины успевают
        # пройти собственную проверку счётчика — тот же зазор, что открыл
        # PR #35 в auth.py, перенеся verify_password в asyncio.to_thread.
        await asyncio.sleep(0.02)
        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 300)
        await pipe.execute()
        return 401

    try:
        statuses = await asyncio.gather(*(old_racy_attempt() for _ in range(n)))
        allowed = sum(1 for s in statuses if s == 401)

        assert allowed > MAX_ATTEMPTS, (
            "негативный контроль не воспроизвёл гонку (allowed="
            f"{allowed} <= {MAX_ATTEMPTS}) — тест выше не может считаться "
            "доказательным для этого паттерна нагрузки/окружения"
        )
    finally:
        await r.delete(key)
        await r.aclose()
