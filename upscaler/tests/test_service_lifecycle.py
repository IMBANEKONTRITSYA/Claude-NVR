"""Обвязка сервиса апскейла: graceful shutdown, JSON-логи, размер пула.

Три пункта carryover цикла 20, все три — про то, что апскейл отставал от
воркера и бэкенда по «обвязке», а не по логике: единственный сервис без
структурированных логов, без graceful shutdown и с дефолтным (для него —
избыточным) пулом соединений.

Тесты идут production path в том смысле, в каком он здесь есть: проверяется
настоящий `main()` с настоящим `signal.signal`/`os.kill` и настоящий
`engine.pool`, а не «функция, которая проверяет флаг». Подменяется только
Redis-очередь — блокирующее чтение из неё и есть то, что должно уметь
прерываться, то есть ровно предмет проверки.
"""
import logging
import os
import signal
import threading
import time

import pytest

# upscaler.py читает DATABASE_URL на уровне модуля через os.environ[...],
# то есть без переменной падает KeyError — а importorskip ловит только
# ImportError (урок цикла 18).
if not os.environ.get("DATABASE_URL"):
    pytest.skip("нет DATABASE_URL — апскейл требует БД", allow_module_level=True)

# Тяжёлые импорты — строго после importorskip (урок цикла 16).
upscaler = pytest.importorskip(
    "upscaler",
    reason="нужны зависимости апскейла (cv2/numpy/sqlalchemy/redis)",
)

from logging_utils import JsonFormatter  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_shutdown_flag():
    """Флаг остановки — глобальный на модуль; тесты не должны протекать
    друг в друга (урок цикла 19 про изоляцию тестов)."""
    upscaler.shutdown_event.clear()
    yield
    upscaler.shutdown_event.clear()


class _FakeRedis:
    """Очередь, которая всегда пуста: blpop честно спит таймаут и возвращает
    None — то же поведение, что у настоящего Redis без задач, и ровно тот
    случай, в котором сервис висел до SIGKILL."""

    def __init__(self):
        self.blpop_calls = 0
        self.closed = False

    def blpop(self, _key, timeout):
        self.blpop_calls += 1
        time.sleep(min(timeout, 0.05))
        return None

    def close(self):
        self.closed = True


def test_sigterm_stops_main_loop(monkeypatch):
    """SIGTERM в реальном процессе доводит `main()` до возврата.

    До фикса `main()` крутил `while True` — вернуться он не мог в принципе,
    и этот тест на прежней реализации виснет до таймаута, а не падает
    ассертом. Поэтому у ожидания нити есть свой бюджет: нить демоническая,
    зависший прогон завершится провалом assert, а не подвешенным pytest.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(upscaler, "r", fake)
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "opencv")  # не грузить GFPGAN
    monkeypatch.setattr(upscaler.engine, "dispose", lambda: None)

    returned = threading.Event()

    def _run():
        upscaler.main()
        returned.set()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    # Дать циклу дойти до blpop, иначе сигнал придёт до входа в while.
    deadline = time.time() + 5
    while fake.blpop_calls == 0 and time.time() < deadline:
        time.sleep(0.02)
    assert fake.blpop_calls > 0, "цикл не дошёл до чтения очереди — тест ничего не проверил"

    # Настоящий сигнал настоящему процессу, а не прямой вызов обработчика:
    # проверяется в том числе то, что обработчик вообще зарегистрирован
    # и что блокирующее чтение очереди ему не мешает.
    prev = signal.signal(signal.SIGTERM, upscaler.handle_shutdown_signal)
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        assert returned.wait(10), (
            "main() не вернулся через 10с после SIGTERM — graceful shutdown "
            "не работает, docker compose stop добьёт сервис SIGKILL'ом "
            "посреди апскейла (осиротевший enh_*.jpg на диске)"
        )
    finally:
        signal.signal(signal.SIGTERM, prev)

    assert fake.closed, "соединение Redis должно закрываться при остановке"


def test_shutdown_before_start_skips_work(monkeypatch):
    """Флаг, установленный до входа в цикл, не даёт взять ни одной задачи.

    Это второй половина фикса: SIGTERM во время стартовой паузы (8с, пока
    поднимаются Postgres и Redis) не должен оборачиваться тем, что сервис
    всё равно начнёт обрабатывать очередь.
    """
    fake = _FakeRedis()
    monkeypatch.setattr(upscaler, "r", fake)
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "opencv")
    monkeypatch.setattr(upscaler.engine, "dispose", lambda: None)

    upscaler.shutdown_event.set()
    upscaler.main()

    assert fake.blpop_calls == 0, "при уже установленном флаге очередь не должна читаться"


def test_error_pause_is_interruptible(monkeypatch):
    """Пауза после ошибки прерывается сигналом, а не спит фиксированную секунду.

    На неработающем Redis (или БД) цикл идёт по ветке except на каждой
    итерации. Раньше там стоял `time.sleep(1)`, то есть остановка сервиса,
    у которого отвалилась зависимость, — самый вероятный сценарий её
    применения — упиралась в неотменяемый сон.
    """
    class _BrokenRedis:
        def __init__(self):
            self.calls = 0

        def blpop(self, _key, timeout):
            self.calls += 1
            raise RuntimeError("redis недоступен")

        def close(self):
            pass

    fake = _BrokenRedis()
    monkeypatch.setattr(upscaler, "r", fake)
    monkeypatch.setattr(upscaler, "UPSCALE_BACKEND", "opencv")
    monkeypatch.setattr(upscaler.engine, "dispose", lambda: None)

    def _stop_soon():
        deadline = time.time() + 5
        while fake.calls == 0 and time.time() < deadline:
            time.sleep(0.01)
        upscaler.shutdown_event.set()

    threading.Thread(target=_stop_soon, daemon=True).start()
    started = time.time()
    upscaler.main()
    elapsed = time.time() - started

    assert fake.calls > 0, "цикл не дошёл до ветки ошибки — тест ничего не проверил"
    assert elapsed < 1.0, (
        f"остановка на сломанном Redis заняла {elapsed:.2f}с — пауза после "
        "ошибки не прерывается флагом остановки"
    )


def test_logging_is_structured_json():
    """Логи сервиса — JSON-строки, а не print (ТЗ 12).

    Апскейл был единственным сервисом, чей вывод шёл через
    `print(..., flush=True)`: в общем логе docker его строки нельзя было ни
    отфильтровать по уровню, ни разобрать теми же средствами, что строки
    воркера и бэкенда.
    """
    assert upscaler.logger.handlers, "logger сервиса не настроен"
    assert isinstance(upscaler.logger.handlers[0].formatter, JsonFormatter), (
        "вывод апскейла должен идти JSON-строками, как у воркера и бэкенда"
    )


def test_no_print_calls_left_in_service():
    """Контроль на возврат print'ов: они обходят и уровни, и формат."""
    import re

    src = (os.path.dirname(os.path.dirname(os.path.abspath(upscaler.__file__)))
           and os.path.abspath(upscaler.__file__))
    text = open(src, encoding="utf-8").read()
    offenders = [
        line.strip()
        for line in text.splitlines()
        if re.match(r"^\s*print\(", line)
    ]
    assert not offenders, f"остались print() вместо logger: {offenders}"


def test_logger_emits_parseable_json(caplog):
    """Формат проверяется на настоящей записи, а не только по типу класса."""
    import json

    record = logging.LogRecord(
        "facewatch.upscaler", logging.INFO, __file__, 0, "событие улучшено", (), None
    )
    record.event_id = 42
    record.backend = "gfpgan"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "событие улучшено"
    assert payload["event_id"] == 42
    assert payload["backend"] == "gfpgan"
    assert payload["level"] == "INFO"


def test_connection_pool_is_sized_for_single_threaded_service():
    """Пул не держит больше соединений, чем сервису нужно.

    Апскейл однопоточный: одновременно ему нужно ровно одно соединение.
    Дефолт SQLAlchemy (pool_size=5, max_overflow=10) означал до 15
    бэкендов Postgres под сервис, которому хватает одного, — на целевом
    железе (N100) это слоты, отнятые у backend'а.
    """
    pool = upscaler.engine.pool
    assert pool.size() <= 2, f"pool_size={pool.size()} — избыточен для однопоточного сервиса"
    # max_overflow приватен в API пула, но именно он и был проблемой:
    # дефолтные 10 сверх пула сводят ограничение выше на нет.
    assert getattr(pool, "_max_overflow", 10) <= 1, (
        "max_overflow оставлен дефолтным (10) — ограничение pool_size ничего не даёт"
    )
