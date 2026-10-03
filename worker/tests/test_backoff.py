"""Юнит-тест экспоненциального бэкоффа RTSP-переподключения (ТЗ 12).
Не тянет тяжёлые ML-зависимости воркера (cv2/insightface) — только чистая
функция из backoff.py."""
from backoff import reconnect_delay, RECONNECT_BASE_DELAY_SEC, RECONNECT_MAX_DELAY_SEC


def test_first_attempt_uses_base_delay():
    assert reconnect_delay(0) == RECONNECT_BASE_DELAY_SEC


def test_delay_doubles_each_attempt():
    assert reconnect_delay(1) == RECONNECT_BASE_DELAY_SEC * 2
    assert reconnect_delay(2) == RECONNECT_BASE_DELAY_SEC * 4
    assert reconnect_delay(3) == RECONNECT_BASE_DELAY_SEC * 8


def test_delay_is_capped():
    assert reconnect_delay(20) == RECONNECT_MAX_DELAY_SEC
    assert reconnect_delay(1000) == RECONNECT_MAX_DELAY_SEC


def test_negative_attempt_treated_as_zero():
    assert reconnect_delay(-5) == RECONNECT_BASE_DELAY_SEC


def test_monotonically_non_decreasing():
    delays = [reconnect_delay(a) for a in range(10)]
    assert delays == sorted(delays)
