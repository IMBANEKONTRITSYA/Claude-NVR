"""Регрессионный тест на находку нагрузочного теста (loadtest/locustfile.py,
SPEC.md §14: "Нагрузочное тестирование... проверка стабильности FPS и
задержек"). При 16 одновременных клиентах p99 задержки на никак не связанных
с /login эндпоинтах (снепшоты, дашборд, архив) подскакивал до 300-900мс
именно в моменты конкурентных входов: verify_password()/hash_password() —
синхронный CPU-bound bcrypt, вызванный напрямую внутри async-обработчика,
блокирует единственный event loop uvicorn на всё время подсчёта хэша, ставя
в очередь вообще все остальные запросы, а не только сам login.
verify_password_async/hash_password_async (app/auth.py) переносят этот
подсчёт в threadpool через asyncio.to_thread — этот файл проверяет и
корректность результата, и (главное) что event loop при этом действительно
остаётся отзывчивым."""
import asyncio

import pytest

from app.auth import hash_password, hash_password_async, verify_password, verify_password_async

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_verify_password_async_matches_sync_result():
    h = hash_password("СложныйПароль123")
    assert await verify_password_async("СложныйПароль123", h) is True
    assert await verify_password_async("неверный", h) is False


async def test_hash_password_async_produces_verifiable_bcrypt_hash():
    h = await hash_password_async("НовыйПароль1")
    assert h.startswith("$2"), "Должен использоваться bcrypt, как и синхронный hash_password"
    assert await verify_password_async("НовыйПароль1", h) is True


async def _run_ticker_around(blocking_call):
    """Запускает независимую корутину-тикер, дающую управление обратно loop'у
    каждые 10мс, параллельно с blocking_call() — возвращает, сколько тиков
    накопилось за время выполнения blocking_call."""
    ticks = 0

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0.01)

    task = asyncio.create_task(ticker())
    await asyncio.sleep(0.02)  # дать тикеру стартовать до начала измерения
    ticks_before = ticks
    await blocking_call()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return ticks - ticks_before


async def test_verify_password_async_does_not_block_event_loop():
    h = hash_password("Секрет123")

    ticks_during = await _run_ticker_around(lambda: verify_password_async("Секрет123", h))

    assert ticks_during >= 2, (
        "event loop не отдавал управление независимой корутине во время "
        "bcrypt — verify_password_async блокирует так же, как синхронный "
        "вызов verify_password() напрямую из async-обработчика"
    )


async def test_control_sync_verify_password_blocks_event_loop():
    """Негативный контроль для теста выше (тот же паттерн, что CSP worker-src
    тест nginx, см. docs/reviews/REVIEW-2026-08-04T204410Z.md): без
    asyncio.to_thread тикер почти не должен успевать тикнуть — доказывает,
    что предыдущий тест действительно чувствителен к блокировке loop'а, а не
    проходит вне зависимости от того, блокирует функция его или нет."""

    async def call_sync_directly():
        # Намеренно НЕ await asyncio.to_thread — синхронный вызов внутри
        # корутины, ровно то, что было до фикса.
        verify_password("Секрет123", hash_password("Секрет123"))

    ticks_during = await _run_ticker_around(call_sync_directly)

    assert ticks_during <= 1, (
        "тикер продолжал тикать во время синхронного bcrypt-вызова — "
        "негативный контроль не воспроизводит блокировку, тест выше "
        "не может считаться доказательным"
    )
