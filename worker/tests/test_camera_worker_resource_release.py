"""Освобождение ресурсов нити камеры на всех путях выхода (P1, цикл 23).

Полный проход воркера по 24/7-стабильности (последний был в цикле 15)
нашёл, что освобождение ресурсов в `camera_worker()` было расписано по
каждой точке выхода по отдельности. Из этого следовали две дыры:

1. **Необработанное исключение в цикле кадров** уносило нить мимо всех этих
   точек, и `cv2.VideoCapture` оставался открытым. `manager()` поднимает
   камеру заново через ~10 с, поэтому утечка накапливалась по одному
   захвату на каждый сбой.

   Цикл 23 проверял здесь же и осиротевший процесс ffmpeg-репабликации;
   с цикла 24 нить камеры его не запускает вовсе — основной поток тянет
   MediaMTX (SPEC §20), — поэтому проверять стало нечего.

   Под try были только распознавание и `process_faces()` — то есть ровно
   то, что уже считали опасным. Незащищёнными оставались обращения к БД:
   `load_cam_state()` ходит в Postgres каждые 10 секунд с каждой нити.
   Обычная для 24/7 перезагрузка БД (обновление, отработка отказа,
   исчерпание пула) роняет все 16 нитей разом — и разом же оставляет 16
   открытых захватов.

2. **Отключение камеры в админке** освобождало захват, но не
   ONVIF-нить: у неё не было своего условия остановки, только глобальный
   `shutdown_event`. Нить оставалась висеть с PullPoint-подпиской и
   сокетом, а повторное включение камеры добавляло рядом ещё одну. Тот же
   класс утечки, что цикл 16 закрыл для случая «RTSP не открывается», но
   на другом пути выхода.

Тесты проверяют поведение (ресурс отпущен, нить остановлена), а не форму
реализации, — чтобы не проходить на коде, который просто переставил вызовы.

Требуют полный requirements.txt воркера для импорта `worker.py`; CI-джоба
worker ставит только лёгкие зависимости, поэтому используется
`importorskip` со всеми тяжёлыми импортами строго после него (урок цикла
16). Проверяются в циклах аудита с полным requirements.txt.
"""
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

import numpy as np  # noqa: E402

ONVIF_CONFIG = {"host": "192.168.1.64", "port": 80, "username": "admin", "password": "s3cret"}


class _WorkingCapture:
    """Открывается и отдаёт настоящие кадры — чтобы цикл дошёл до
    периодического `load_cam_state()`, а не завис на реконнекте."""

    def __init__(self):
        self.released = False

    def isOpened(self):
        return True

    def get(self, prop_id):
        return 25.0

    def read(self):
        return True, np.zeros((360, 640, 3), dtype=np.uint8)

    def release(self):
        self.released = True


@pytest.fixture()
def harness(monkeypatch):
    """Подменяет только границы процесса: захват RTSP, запись на диск и
    статус в БД. Сам `camera_worker()` выполняется настоящий."""
    cap = _WorkingCapture()
    monkeypatch.setattr(worker, "open_capture", lambda url: cap)
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "save_latest_frame", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "resolve_snapshot_url", lambda *a, **kw: None)
    return cap


def test_resources_released_when_frame_loop_raises(harness, monkeypatch):
    """Сбой обращения к БД в цикле кадров не должен утекать захватом.

    Воспроизводит production path: `load_cam_state()` вызывается настоящим
    циклом кадров и падает так, как падал бы при недоступном Postgres.
    """
    cap = harness

    def boom(cam_id):
        raise RuntimeError("БД недоступна")

    monkeypatch.setattr(worker, "load_cam_state", boom)

    # Нить не должна разваливать процесс: исключение логируется и гасится,
    # manager() поднимет камеру заново.
    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url="rtsp://cam/sub")

    assert cap.released, (
        "RTSP-захват OpenCV не освобождён при исключении в цикле кадров — "
        "каждый сбой оставляет открытый захват, manager() создаёт рядом новый"
    )


def test_onvif_thread_stops_when_camera_disabled(harness, monkeypatch):
    """Отключение камеры в админке должно останавливать и ONVIF-нить.

    Нить запускается настоящая (`onvif_poll_worker` не подменяется);
    подменяется только сетевой вызов ONVIF — то есть проверяется реальное
    условие остановки нити, а не факт вызова.
    """
    cap = harness
    polling = threading.Event()

    def fake_subscribe(host, port, username, password):
        polling.set()
        return "http://camera/subscription-1"

    def fake_pull(url, username, password):
        return []

    monkeypatch.setattr(worker.onvif_client, "create_pull_point_subscription", fake_subscribe)
    monkeypatch.setattr(worker.onvif_client, "pull_messages", fake_pull)

    # Первый вызов (до цикла) — камера активна; последующие — отключена.
    calls = {"n": 0}

    def state(cam_id):
        calls["n"] += 1
        return (None, calls["n"] <= 1, None)

    monkeypatch.setattr(worker, "load_cam_state", state)

    before = set(threading.enumerate())
    worker.camera_worker(1, "rtsp://cam/main", face_app=None,
                         sub_rtsp_url="rtsp://cam/sub", onvif_config=ONVIF_CONFIG)

    assert polling.wait(5), "ONVIF-нить не стартовала — тест ничего не проверил"
    # Нить должна уйти сама, без глобального shutdown_event.
    assert not worker.shutdown_event.is_set(), "глобальный shutdown не должен быть выставлен"

    waiter = threading.Event()
    for _ in range(50):
        alive = [t for t in threading.enumerate() if t not in before and t.is_alive()]
        if not alive:
            break
        waiter.wait(0.1)

    alive = [t for t in threading.enumerate() if t not in before and t.is_alive()]
    assert not alive, (
        f"после остановки камеры остались нити {[t.name for t in alive]} — "
        "ONVIF-нить продолжает держать PullPoint-подписку и сокет; "
        "повторное включение камеры добавит рядом ещё одну"
    )
    assert cap.released
