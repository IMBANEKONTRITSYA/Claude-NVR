"""Юнит-тест координации graceful shutdown (ТЗ 12). Не тянет тяжёлые
ML-зависимости воркера — только чистая логика из shutdown.py."""
import threading
import importlib

import shutdown as shutdown_mod


def _fresh_module():
    """Каждый тест получает не-засорённый shutdown_event: перезагружаем
    модуль, а не полагаемся на порядок выполнения тестов."""
    return importlib.reload(shutdown_mod)


def test_event_starts_unset():
    mod = _fresh_module()
    assert not mod.shutdown_event.is_set()


def test_handler_sets_event():
    mod = _fresh_module()
    assert not mod.shutdown_event.is_set()
    mod.handle_shutdown_signal(15, None)  # SIGTERM = 15
    assert mod.shutdown_event.is_set()


def test_event_is_visible_across_threads():
    mod = _fresh_module()
    seen = threading.Event()

    def waiter():
        if mod.shutdown_event.wait(timeout=2.0):
            seen.set()

    t = threading.Thread(target=waiter)
    t.start()
    mod.handle_shutdown_signal(2, None)  # SIGINT = 2
    t.join(timeout=3.0)
    assert seen.is_set(), "Нити должны видеть shutdown_event.set() без опроса с задержкой"
