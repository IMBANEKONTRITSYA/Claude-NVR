"""Регрессионный тест бэкоффа авто-перезапуска ffmpeg-репабликации (P1,
известный пробел с цикла 15, закрыт циклом 16 — см. docs/reviews/REVIEW_LOG.md).

До фикса camera_worker() проверял `republish.poll() is not None` (процесс
умер) на каждой итерации цикла кадров и безусловно перезапускал ffmpeg —
для камеры с постоянно недоступным основным RTSP-потоком (неверный
пароль/URL, отключённая камера) это означало попытку заново поднять ffmpeg
несколько раз в секунду: fork/connect-storm без всякой пользы, пока RTSP не
восстановится сам. Здесь имитируем ffmpeg, который умирает мгновенно после
каждого запуска, и проверяем, что число фактических перезапусков за
фиксированное окно ограничено экспоненциальным бэкоффом, а не растёт
неограниченно с частотой кадров.

Требует полный requirements.txt воркера (см. докстринг
test_camera_worker_onvif_thread.py) — pytest.importorskip как и там."""
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.), см. докстринг модуля"
)


class _DeadProcess:
    """Имитирует ffmpeg.Popen, который уже завершился (упал)."""

    def poll(self):
        return 1  # любое не-None значение — «процесс завершён»

    def terminate(self):
        pass


class _FramesThenShutdownCapture:
    """Отдаёт кадры (в цикле обработки они дальше не нужны — motion/faces
    заглушены), пока тест не остановит воркер через shutdown_event."""

    def isOpened(self):
        return True

    def get(self, prop_id):
        return 25.0

    def read(self):
        # (True, None) — cap считается «живым», кадр обработке ниже не важен
        # благодаря заглушенным cv2.createBackgroundSubtractorMOG2/detect ниже
        return True, worker.np.zeros((10, 10, 3), dtype=worker.np.uint8)

    def release(self):
        pass


def test_republish_restart_is_backed_off_not_per_frame(monkeypatch):
    restart_calls = []

    def fake_start_republish(cam_id, rtsp_url):
        restart_calls.append(worker.time.time())
        return _DeadProcess()

    monkeypatch.setattr(worker, "start_republish", fake_start_republish)
    monkeypatch.setattr(worker, "open_capture", lambda url: _FramesThenShutdownCapture())
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "load_cam_state", lambda cam_id: (None, True, None))
    # bg-вычитание/детекция лиц не нужны для этого теста — важен только
    # путь авто-перезапуска republish, не остальной цикл кадра.
    monkeypatch.setattr(
        worker.cv2, "createBackgroundSubtractorMOG2",
        lambda *a, **kw: type("Bg", (), {"apply": lambda self, frame: worker.np.zeros((10, 10), dtype=worker.np.uint8)})(),
    )

    t = threading.Thread(
        target=worker.camera_worker,
        args=(1, "rtsp://cam/main", None),
        kwargs={"sub_rtsp_url": None, "onvif_config": None},
        daemon=True,
    )
    t.start()
    try:
        # Даём воркеру прогнать цикл кадров ~0.3с — на исходном коде (без
        # бэкоффа) это десятки/сотни итераций при cap.read(), возвращающем
        # кадр мгновенно, то есть десятки/сотни рестартов republish.
        worker.shutdown_event.wait(0.3)
    finally:
        worker.shutdown_event.set()
        t.join(timeout=5)
        worker.shutdown_event.clear()

    # Единственный вызов — начальный spawn republish перед циклом кадров;
    # базовая задержка бэкоффа (2с) не даёт случиться перезапуску внутри
    # цикла кадров в пределах 0.3с окна теста, даже если ffmpeg падает
    # мгновенно после самого первого запуска.
    assert len(restart_calls) == 1, (
        f"ожидался ровно один перезапуск ffmpeg-репабликации в пределах базовой задержки "
        f"бэкоффа, получено {len(restart_calls)} — рестарт не должен происходить на "
        f"каждой итерации цикла кадров"
    )


def test_republish_restart_resumes_after_backoff_window(monkeypatch):
    """Бэкофф не должен «залипать» навсегда — после истечения задержки
    попытки перезапуска возобновляются (просто не на каждом кадре)."""
    restart_calls = []

    def fake_start_republish(cam_id, rtsp_url):
        restart_calls.append(worker.time.time())
        return _DeadProcess()

    monkeypatch.setattr(worker, "start_republish", fake_start_republish)
    monkeypatch.setattr(worker, "open_capture", lambda url: _FramesThenShutdownCapture())
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "load_cam_state", lambda cam_id: (None, True, None))
    monkeypatch.setattr(
        worker.cv2, "createBackgroundSubtractorMOG2",
        lambda *a, **kw: type("Bg", (), {"apply": lambda self, frame: worker.np.zeros((10, 10), dtype=worker.np.uint8)})(),
    )
    # Сжимаем бэкофф до 0.05с вместо реальных 2с/4с/8с — иначе тест на
    # «перезапуск возобновляется» пришлось бы ждать секундами.
    monkeypatch.setattr(worker, "reconnect_delay", lambda attempt: 0.05)

    t = threading.Thread(
        target=worker.camera_worker,
        args=(1, "rtsp://cam/main", None),
        kwargs={"sub_rtsp_url": None, "onvif_config": None},
        daemon=True,
    )
    t.start()
    try:
        worker.shutdown_event.wait(0.3)
    finally:
        worker.shutdown_event.set()
        t.join(timeout=5)
        worker.shutdown_event.clear()

    # За 0.3с окна с задержкой 0.05с между попытками должно накопиться
    # несколько (но не сотни/тысячи, как было бы без бэкоффа вовсе)
    # перезапусков — подтверждает, что попытки возобновляются, а не
    # блокируются навсегда после первого рестарта.
    assert 2 <= len(restart_calls) <= 20, (
        f"ожидалось несколько (2-20) перезапусков за 0.3с с задержкой бэкоффа 0.05с, "
        f"получено {len(restart_calls)}"
    )
