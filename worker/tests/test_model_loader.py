"""Нить загрузки модели: SPEC §2 «падение/деградация одного слоя не
затрагивает другой» и §15 «профиль применяется ТОЛЬКО к слою аналитики».

`test_model_failure_isolation.py` рядом проверяет **падение** загрузки:
отказ возвращает False, а не летит наверх. Здесь проверяется второе
слагаемое той же строки ТЗ — **деградация**: медленная загрузка не должна
останавливать слой записи. Приём различения этих двух случаев разобран в
отчёте цикла 55.

Набор на голом stdlib и идёт в лёгкой джобе CI: `model_loader.py` не
импортирует ни cv2, ни insightface намеренно.
"""
import threading
import time

import pytest

from model_loader import ERROR, IDLE, LOADING, READY, ModelLoader


class FakeClock:
    """Управляемое время: ждать реальные 300 секунд паузы повтора нельзя."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


def _loader(load, **kw):
    kw.setdefault("poll_sec", 0.01)
    return ModelLoader(load, **kw)


def _wait(pred, timeout=5.0):
    """Ждёт условие, не усыпляя тест на фиксированный срок."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.005)
    return False


# --- главное: менеджер не ждёт загрузку -----------------------------------

def test_request_returns_while_the_load_is_still_running():
    """`request()` не блокирует — в этом весь смысл модуля.

    Пока загрузка вызывалась синхронно из manager(), возврата из неё ждал
    весь управляющий контур слоя записи: синхронизация путей MediaMTX,
    индексация сегментов, статусы потоков, циклическая перезапись.
    """
    started = threading.Event()
    release = threading.Event()

    def slow_load(params):
        started.set()
        release.wait(10)
        return True

    ld = _loader(slow_load)
    ld.start()
    try:
        t0 = time.monotonic()
        ld.request(("buffalo_s", 640, 1))
        assert time.monotonic() - t0 < 0.5, "request() ждал загрузку"
        assert started.wait(5), "нить загрузки не начала работу"

        # Загрузка идёт прямо сейчас — и это видно снаружи, а не только
        # по отсутствию модели.
        assert _wait(lambda: ld.snapshot()["state"] == LOADING)
        # Повторный вызов с теми же параметрами тоже мгновенный: менеджер
        # зовёт его каждые 10 секунд.
        t0 = time.monotonic()
        ld.request(("buffalo_s", 640, 1))
        assert time.monotonic() - t0 < 0.5
    finally:
        release.set()
        ld.stop()


def test_slow_load_eventually_becomes_ready():
    """Позитивный контроль: не ждать — не значит потерять результат."""
    release = threading.Event()

    def slow_load(params):
        release.wait(10)
        return True

    ld = _loader(slow_load)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == LOADING)
        release.set()
        assert _wait(lambda: ld.snapshot()["state"] == READY)
        assert ld.snapshot()["model"] == "buffalo_s"
    finally:
        ld.stop()


# --- состояние для §9 ------------------------------------------------------

def test_idle_until_asked():
    ld = _loader(lambda p: True)
    assert ld.snapshot()["state"] == IDLE
    assert ld.snapshot()["model"] is None


def test_loading_reports_the_model_being_loaded_not_the_previous_one():
    """При смене профиля §9 обязан назвать НОВУЮ модель.

    Иначе строка «грузится 40 с» относилась бы к прежней модели, и
    администратор, только что переключивший профиль, видел бы старое имя.
    """
    release = threading.Event()
    calls = []

    def load(params):
        calls.append(params)
        if len(calls) > 1:
            release.wait(10)
        return True

    ld = _loader(load)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == READY)
        ld.request(("buffalo_l", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == LOADING)
        assert ld.snapshot()["model"] == "buffalo_l"
    finally:
        release.set()
        ld.stop()


def test_error_state_carries_the_reason_from_the_worker():
    """Текст отказа принадлежит воркеру (`MODEL_ERROR`) — модуль его не
    сочиняет, а берёт, чтобы у одной строки интерфейса не стало двух
    источников."""
    ld = _loader(lambda p: False, error_getter=lambda: "ConnectionError: нет сети")
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == ERROR)
        assert ld.snapshot()["error"] == "ConnectionError: нет сети"
    finally:
        ld.stop()


def test_ready_state_does_not_carry_a_stale_error():
    """Удачная дозагрузка снимает старое сообщение об аварии."""
    ld = _loader(lambda p: True, error_getter=lambda: "старая авария")
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == READY)
        assert ld.snapshot()["error"] is None
    finally:
        ld.stop()


# --- повтор после отказа ---------------------------------------------------

def test_failed_load_is_retried_only_after_the_pause():
    """Повтор раз в `retry_sec`, а не в каждом обороте нити.

    Без паузы изолированный сервер молотил бы попытками непрерывно.
    """
    clock = FakeClock()
    calls = []
    ld = _loader(lambda p: calls.append(p) or False, retry_sec=300.0, clock=clock)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: len(calls) == 1)

        clock.advance(299)
        time.sleep(0.05)
        assert len(calls) == 1, "повтор раньше срока"

        clock.advance(2)
        assert _wait(lambda: len(calls) == 2)
    finally:
        ld.stop()


def test_new_parameters_retry_immediately_without_waiting_out_the_pause():
    """Смена профиля в админке применяется сразу.

    Пауза повтора отсчитана для ПРОШЛОЙ модели: заставлять администратора
    ждать её конца значило бы, что кнопка «Применить» иногда работает
    через пять минут.
    """
    clock = FakeClock()
    calls = []
    ld = _loader(lambda p: calls.append(p) or False, retry_sec=300.0, clock=clock)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: len(calls) == 1)

        ld.request(("buffalo_l", 640, 1))
        assert _wait(lambda: len(calls) == 2)
        assert calls[1] == ("buffalo_l", 640, 1)
    finally:
        ld.stop()


def test_already_loaded_parameters_are_not_reloaded():
    """Менеджер зовёт `request()` каждые 10 секунд — перезагружать модель
    на каждый вызов означало бы непрерывную загрузку."""
    calls = []
    ld = _loader(lambda p: calls.append(p) or True)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: len(calls) == 1)
        for _ in range(5):
            ld.request(("buffalo_s", 640, 1))
        time.sleep(0.1)
        assert len(calls) == 1
    finally:
        ld.stop()


def test_ort_thread_budget_change_reloads_the_model():
    """Бюджет потоков ORT — часть параметров: он применяется к сессиям при
    создании, и смена настройки без перезагрузки не изменила бы ничего
    (SPEC §16)."""
    calls = []
    ld = _loader(lambda p: calls.append(p) or True)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: len(calls) == 1)
        ld.request(("buffalo_s", 640, 2))
        assert _wait(lambda: len(calls) == 2)
    finally:
        ld.stop()


# --- живучесть нити --------------------------------------------------------

def test_thread_survives_an_unexpected_exception_from_the_loader():
    """Смерть нити означала бы аналитику, молча выключенную до перезапуска
    процесса. `_try_load_model()` исключений не выпускает, но нить обязана
    пережить и то, чего «не бывает»."""
    calls = []

    def load(params):
        calls.append(params)
        if len(calls) == 1:
            raise RuntimeError("нечто непредвиденное")
        return True

    clock = FakeClock()
    ld = _loader(load, retry_sec=10.0, clock=clock)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == ERROR)
        assert ld.is_alive()
        clock.advance(11)
        assert _wait(lambda: ld.snapshot()["state"] == READY)
    finally:
        ld.stop()


def test_stop_event_ends_the_thread():
    stop = threading.Event()
    ld = _loader(lambda p: True, stop_event=stop)
    ld.start()
    ld.stop()
    ld.join(timeout=5)
    assert not ld.is_alive()


# --- предупреждение о затянувшейся загрузке -------------------------------

def test_slow_load_is_reported_once_not_every_pass(caplog):
    """Предупреждение пишет менеджер (нить загрузки в этот момент сидит в
    `requests.get()` без таймаута) — и ровно один раз на попытку, иначе
    оно заполнило бы журнал за час простоя."""
    clock = FakeClock()
    release = threading.Event()
    ld = _loader(lambda p: release.wait(10), clock=clock)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == LOADING)

        assert ld.log_if_slow() is False, "предупредил раньше срока"
        clock.advance(121)
        assert ld.log_if_slow() is True
        assert ld.log_if_slow() is False, "повторное предупреждение"
    finally:
        release.set()
        ld.stop()


def test_slow_warning_arms_again_for_the_next_attempt():
    """Вторая затянувшаяся попытка обязана предупредить снова: иначе
    единственная строка за всю жизнь процесса ничего не говорит о том,
    что происходит сейчас."""
    clock = FakeClock()
    hold = threading.Event()
    attempts = []

    def load(params):
        attempts.append(params)
        hold.wait(10)
        return False

    ld = _loader(load, retry_sec=1.0, clock=clock)
    ld.start()
    try:
        ld.request(("buffalo_s", 640, 1))
        assert _wait(lambda: ld.snapshot()["state"] == LOADING)
        clock.advance(121)
        assert ld.log_if_slow() is True

        hold.set()
        assert _wait(lambda: ld.snapshot()["state"] == ERROR)
        hold.clear()
        clock.advance(2)
        assert _wait(lambda: len(attempts) == 2)
        assert _wait(lambda: ld.snapshot()["state"] == LOADING)
        clock.advance(121)
        assert ld.log_if_slow() is True
    finally:
        hold.set()
        ld.stop()
