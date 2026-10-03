"""Сторож живости бэкенда — класс отказа «сервис жив и не отвечает» (§13/§19).

**Что закрывается.** Docker не перезапускает контейнер по проваленному
healthcheck: он метит его `unhealthy`, и на `restart: unless-stopped` это
не влияет никак. Бэкенд с заблокированным циклом событий оставался в этом
состоянии сколько угодно — интерфейс не открывается, live не идёт, а §19
требует RTO ≤ 5 минут. Между «нездоров» и «перезапущен» не было ничего.

Решение сторожа проверяется на управляемых часах, а не реальным ожиданием
бюджета: тест, ждущий 60 секунд, в CI не живёт. Отдельно проверяется, что
отметчик и правда отмечается в настоящем цикле событий — иначе тесты
подтверждали бы только арифметику порогов.
"""
import asyncio
import threading

import pytest

from app import liveness


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, sec):
        self.now += sec


def _wd(hb, clock, budget=60.0, grace=60.0):
    tripped = []
    wd = liveness.LoopWatchdog(hb, lag_budget_sec=budget, kill_grace_sec=grace,
                               clock=clock, on_trip=lambda lag: tripped.append(lag))
    return wd, tripped


# --- LoopHeartbeat ---------------------------------------------------------


def test_lag_grows_until_beat():
    clock = FakeClock()
    hb = liveness.LoopHeartbeat(clock=clock)
    clock.advance(5.0)
    assert hb.lag() == pytest.approx(5.0)
    hb.beat()
    assert hb.lag() == pytest.approx(0.0)


# --- решение сторожа -------------------------------------------------------


def test_live_loop_does_not_trip():
    """Позитивный контроль: работающий бэкенд не перезапускается.

    Без него «фикс», убивающий процесс всегда, прошёл бы весь набор.
    """
    clock = FakeClock()
    hb = liveness.LoopHeartbeat(clock=clock)
    wd, tripped = _wd(hb, clock)
    for _ in range(200):
        hb.beat()
        clock.advance(1.0)
        assert wd.check_once() is False
    assert tripped == []


def test_trips_only_after_budget_plus_grace():
    clock = FakeClock()
    hb = liveness.LoopHeartbeat(clock=clock)
    wd, tripped = _wd(hb, clock, budget=60.0, grace=60.0)

    clock.advance(61.0)              # бюджет вышел — но это ещё не рестарт
    assert wd.check_once() is False
    assert tripped == []

    clock.advance(59.0)              # отсрочка не истекла
    assert wd.check_once() is False

    clock.advance(2.0)
    assert wd.check_once() is True
    assert tripped and tripped[0] >= 120.0


def test_long_but_finite_block_does_not_restart():
    """Разовая долгая синхронная операция — запись в журнале, не рестарт.

    Ровно ради этого у сторога два порога, а не один: выгрузка отчёта или
    миграция могут заблокировать цикл на минуту, и менять это на перезапуск
    сервиса нельзя.
    """
    clock = FakeClock()
    hb = liveness.LoopHeartbeat(clock=clock)
    wd, tripped = _wd(hb, clock, budget=60.0, grace=60.0)

    clock.advance(90.0)
    assert wd.check_once() is False   # замечено
    hb.beat()                         # операция закончилась, цикл ожил
    clock.advance(1.0)
    assert wd.check_once() is False
    assert tripped == []


def test_recovery_resets_the_grace():
    """Отсрочка отсчитывается заново, иначе второй эпизод убивал бы сразу."""
    clock = FakeClock()
    hb = liveness.LoopHeartbeat(clock=clock)
    wd, tripped = _wd(hb, clock, budget=60.0, grace=60.0)

    clock.advance(61.0)
    wd.check_once()
    clock.advance(50.0)
    wd.check_once()

    hb.beat()
    clock.advance(1.0)
    assert wd.check_once() is False

    clock.advance(61.0)
    wd.check_once()
    clock.advance(50.0)
    assert wd.check_once() is False, "отсрочка обязана начаться заново"
    assert tripped == []


def test_watchdog_survives_exception_in_check():
    """Падение нити сторожа тихое — поэтому она не имеет права падать."""
    class Exploding:
        def lag(self):
            raise RuntimeError("bang")

    wd = liveness.LoopWatchdog(Exploding(), lag_budget_sec=1.0, kill_grace_sec=1.0,
                               interval_sec=0.01, on_trip=lambda lag: None)
    wd.start()
    import time
    time.sleep(0.1)
    alive = wd.is_alive()
    wd.stop()
    wd.join(timeout=3.0)
    assert alive


def test_stop_ends_the_thread():
    wd, _ = _wd(liveness.LoopHeartbeat(), FakeClock())
    wd.stop()
    wd.start()
    wd.join(timeout=3.0)
    assert not wd.is_alive()


# --- сторож живёт в нити, а не в цикле событий -----------------------------


def test_watchdog_sees_a_really_blocked_event_loop():
    """Главная проверка: сторож обязан работать, когда цикл событий встал.

    Сторож, живущий внутри цикла событий, при его блокировке замолчал бы
    вместе с ним — то есть не сработал бы ровно в том случае, ради которого
    существует. Здесь цикл блокируется по-настоящему (`time.sleep` внутри
    корутины), а проверка идёт из нити.
    """
    import time

    hb = liveness.LoopHeartbeat()
    seen = []
    stop_thread = threading.Event()

    def watcher():
        while not stop_thread.is_set():
            seen.append(hb.lag())
            time.sleep(0.02)

    async def scenario():
        stop = asyncio.Event()
        beat = asyncio.create_task(liveness.beat_loop(hb, stop, interval=0.01))
        await asyncio.sleep(0.1)          # цикл жив, отметки идут
        healthy = max(seen)
        time.sleep(0.4)                   # цикл событий заблокирован НАМЕРТВО
        blocked = max(seen)
        stop.set()
        await beat
        return healthy, blocked

    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    try:
        healthy, blocked = asyncio.run(scenario())
    finally:
        stop_thread.set()
        t.join(timeout=2.0)

    assert healthy < 0.1, "на живом цикле отставание обязано оставаться малым"
    assert blocked > 0.3, (
        "нить обязана видеть рост отставания, пока цикл событий заблокирован — "
        "иначе сторож не заметил бы именно тот отказ, ради которого заведён"
    )


def test_beat_loop_stops_on_event():
    """Отметчик обязан завершаться по событию, иначе завершение зависнет."""
    async def scenario():
        hb = liveness.LoopHeartbeat()
        stop = asyncio.Event()
        task = asyncio.create_task(liveness.beat_loop(hb, stop, interval=0.01))
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())


# --- настройки окружения ---------------------------------------------------


def test_can_be_disabled(monkeypatch):
    monkeypatch.setenv("BACKEND_WATCHDOG_ENABLED", "0")
    assert liveness.start_watchdog(liveness.LoopHeartbeat()) is None


def test_broken_env_falls_back(monkeypatch):
    monkeypatch.setenv("BACKEND_WATCHDOG_LAG_BUDGET_SEC", "не-число")
    assert liveness._env_float("BACKEND_WATCHDOG_LAG_BUDGET_SEC", 60.0) == 60.0


def test_exit_code_is_distinct():
    assert liveness.EXIT_STALLED not in (0, 1)
