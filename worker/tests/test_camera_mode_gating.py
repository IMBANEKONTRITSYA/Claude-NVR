"""Слой аналитики уважает режим камеры (SPEC §2, §6, §24) — цикл 24.

Слой записи берёт все включённые камеры, слой аналитики — только те, что в
режиме `analytics`. Проверяется поведение, а не форма: нить камеры,
переведённой в `record_only`, обязана завершиться сама, без перезапуска
воркера (SPEC §2: «Переключение режима ... без перезапуска слоёв»).

Мокается только граница процесса — захват RTSP и статус в БД; сам
`camera_worker()` и `load_cam_state()` выполняются настоящие, а состояние
камеры читается из настоящего запроса к БД, подменённого на уровне
`Session`.

Требуют полный requirements.txt воркера для импорта `worker.py` (урок цикла
16: все тяжёлые импорты строго после `importorskip`).
"""
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

import numpy as np  # noqa: E402


class _FakeCamera:
    def __init__(self, enabled=True, mode="analytics"):
        self.enabled = enabled
        self.mode = mode
        self.roi = None
        self.motion_sensitivity = None


class _FakeSession:
    """Контекст-менеджер с одним `get()` — ровно то, что использует
    `load_cam_state()`."""

    def __init__(self, cam):
        self._cam = cam

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, model, cam_id):
        return self._cam


def _session_factory(cam):
    return lambda: _FakeSession(cam)


def test_record_only_camera_is_not_active_for_analytics(monkeypatch):
    """SPEC §6: детекция только на камерах analytics.

    `load_cam_state()` — единственное место, через которое нить камеры
    узнаёт, что ей пора остановиться. Режим обязан влиять на него так же,
    как флаг «камера отключена».
    """
    monkeypatch.setattr(worker, "Session", _session_factory(_FakeCamera(mode="record_only")))
    _roi, active, _sens = worker.load_cam_state(1)
    assert active is False


def test_analytics_camera_is_active(monkeypatch):
    monkeypatch.setattr(worker, "Session", _session_factory(_FakeCamera(mode="analytics")))
    _roi, active, _sens = worker.load_cam_state(1)
    assert active is True


def test_camera_without_mode_column_is_treated_as_record_only(monkeypatch):
    """БД, ещё не прошедшая миграцию (воркер стартовал раньше бэкенда),
    отдаёт камеру без `mode`. Безопасный вариант — не запускать аналитику:
    лишняя нить на 120 камерах хуже, чем её отсутствие до перезапуска."""
    cam = _FakeCamera()
    del cam.mode
    monkeypatch.setattr(worker, "Session", _session_factory(cam))
    _roi, active, _sens = worker.load_cam_state(1)
    assert active is False


class _WorkingCapture:
    def __init__(self):
        self.released = False

    def isOpened(self):
        return True

    def get(self, prop):
        return 25.0

    def read(self):
        return True, np.zeros((360, 640, 3), dtype=np.uint8)

    def release(self):
        self.released = True


def test_switching_camera_to_record_only_stops_the_analytics_thread(monkeypatch):
    """Production path: нить крутится на настоящем цикле кадров, режим
    камеры меняется «в админке» (в БД) — нить обязана выйти сама и
    освободить захват.

    Именно это делает переключение режима применимым «без перезапуска
    слоёв» (SPEC §2): без него нить продолжала бы детекцию на камере,
    которую перевели только на запись, до перезапуска воркера.
    """
    cam = _FakeCamera(mode="analytics")
    cap = _WorkingCapture()
    monkeypatch.setattr(worker, "Session", _session_factory(cam))
    monkeypatch.setattr(worker, "open_capture", lambda url: cap)
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "save_latest_frame", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "resolve_snapshot_url", lambda *a, **kw: None)
    # Перезагрузка состояния идёт раз в 10 секунд — ускоряем, чтобы тест не
    # ждал реального интервала.
    monkeypatch.setattr(worker, "IDLE_AFTER_SEC", 0)

    t = threading.Thread(
        target=worker.camera_worker,
        args=(1, "rtsp://cam/main", None),
        kwargs={"sub_rtsp_url": "rtsp://cam/sub"},
        daemon=True,
    )
    t.start()
    try:
        # Нить успела зайти в цикл кадров на камере в режиме analytics.
        t.join(timeout=1.0)
        assert t.is_alive(), "нить аналитики не должна завершаться на камере в режиме analytics"

        cam.mode = "record_only"
        t.join(timeout=15.0)
        assert not t.is_alive(), (
            "нить аналитики продолжает работать после перевода камеры в "
            "record_only — переключение режима не применяется без перезапуска "
            "воркера (SPEC §2)"
        )
        assert cap.released, "захват RTSP не освобождён при остановке по смене режима"
    finally:
        worker.shutdown_event.set()
        t.join(timeout=5)
        worker.shutdown_event.clear()
