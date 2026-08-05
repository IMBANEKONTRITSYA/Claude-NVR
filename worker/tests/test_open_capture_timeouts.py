"""Регрессионный тест детекта зависших RTSP-потоков (P1, ТЗ 12: "детект
зависших потоков").

До фикса open_capture() не выставлял CAP_PROP_OPEN_TIMEOUT_MSEC/
CAP_PROP_READ_TIMEOUT_MSEC — ни cap.open(), ни cap.read() не имели
тайм-аута на FFMPEG-бэкенде OpenCV. Классическая поломка RTSP (TCP-сессия
формально открыта — NAT-keepalive, зависшая прошивка камеры — но кадры не
идут) блокировала cap.read() на неопределённое время: существующий
backoff-реконнект (reconnect_delay()) никогда не срабатывал, поток
считался «живым» (заблокирован ≠ мёртв), manager() не перезапускал нить, а
на shutdown join(timeout=...) просто истекал, не освобождая ресурс.

Требует полный requirements.txt воркера (cv2 и т.д.) для импорта worker.py
— см. докстринг test_camera_worker_onvif_thread.py про importorskip и
границы CI."""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.), см. докстринг модуля"
)

import cv2  # noqa: E402 — после importorskip("worker"), который уже гарантирует наличие cv2


class _RecordingCapture:
    """Записывает каждый cap.set()/cap.open() по порядку — проверяем, что
    тайм-ауты выставлены ДО open() (после открытия потока OpenCV их уже не
    применяет, см. комментарий в open_capture())."""

    def __init__(self):
        self.calls = []

    def set(self, prop_id, value):
        self.calls.append(("set", prop_id, value))
        return True

    def open(self, url, backend):
        self.calls.append(("open", url, backend))
        return True


def test_open_capture_sets_open_and_read_timeouts_before_opening(monkeypatch):
    fake = _RecordingCapture()
    monkeypatch.setattr(worker.cv2, "VideoCapture", lambda: fake)

    worker.open_capture("rtsp://cam/sub")

    set_calls = [c for c in fake.calls if c[0] == "set"]
    open_calls = [c for c in fake.calls if c[0] == "open"]
    assert open_calls, "open_capture() должен вызывать cap.open()"

    open_timeout = next((c for c in set_calls if c[1] == cv2.CAP_PROP_OPEN_TIMEOUT_MSEC), None)
    read_timeout = next((c for c in set_calls if c[1] == cv2.CAP_PROP_READ_TIMEOUT_MSEC), None)
    assert open_timeout is not None, "CAP_PROP_OPEN_TIMEOUT_MSEC должен быть выставлен"
    assert read_timeout is not None, "CAP_PROP_READ_TIMEOUT_MSEC должен быть выставлен"
    # Положительные, конечные значения — 0/не выставлено means "без тайм-аута"
    # на большинстве сборок OpenCV, что и было исходной проблемой.
    assert 0 < open_timeout[2] <= 60_000
    assert 0 < read_timeout[2] <= 60_000

    # Оба тайм-аута выставлены строго до open() — свойство, применяемое
    # FFMPEG-бэкендом только на момент самого открытия потока.
    open_index = fake.calls.index(open_calls[0])
    assert fake.calls.index(open_timeout) < open_index
    assert fake.calls.index(read_timeout) < open_index


def test_open_capture_survives_backend_without_timeout_properties(monkeypatch):
    """Сборка OpenCV без поддержки этих свойств не должна ронять
    open_capture() целиком — откат на прежнее поведение (без тайм-аута),
    как и для CAP_PROP_HW_ACCELERATION чуть ниже в той же функции."""

    class _RaisingCapture(_RecordingCapture):
        def set(self, prop_id, value):
            raise cv2.error("свойство не поддерживается этой сборкой")

    fake = _RaisingCapture()
    monkeypatch.setattr(worker.cv2, "VideoCapture", lambda: fake)

    cap = worker.open_capture("rtsp://cam/sub")
    assert cap is fake
    assert ("open", "rtsp://cam/sub", cv2.CAP_FFMPEG) in fake.calls
