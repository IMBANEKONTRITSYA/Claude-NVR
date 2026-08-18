"""Канал аналитики привязывается к своей NUMA-ноде до первого кадра (§17).

`test_cpu_affinity.py` проверяет саму раскладку (какая камера на какую
ноду) и работает на одном stdlib. Здесь проверяется **порядок** внутри
`camera_worker()` — свойство, которое раскладка не задаёт, а нарушение
которого молча обесценивает её целиком.

Порядок важен из-за политики памяти Linux: страница достаётся ноде того
потока, который обратился к ней первым (first-touch). Кадровые буферы
(720p BGR — 2.6 МБ на кадр), фон MOG2 и тензоры детектора заводятся при
открытии захвата и на первых кадрах. Привязка после них закрепила бы за
каналом ядра одной ноды и память другой — то есть ровно ту раскладку,
которую §17 просит устранить, только теперь уже необратимо.

Требует полный requirements.txt воркера (импорт `worker.py` тянет cv2).
"""
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

pytestmark = pytest.mark.skipif(
    not hasattr(os, "sched_setaffinity"),
    reason="привязка к ядрам есть только на Linux",
)


class _ClosedCapture:
    """Захват, который не открылся: `camera_worker()` на этом выходит.

    Ранний выход — самая короткая дорога до нужной точки: привязка обязана
    случиться ДО открытия захвата, значит к моменту вызова `open_capture()`
    она уже должна быть видна.
    """

    def isOpened(self):
        return False


def _run_camera_worker(cpus, monkeypatch):
    """Запустить `camera_worker()` в отдельной нити и вернуть, что видел захват."""
    seen: dict = {}

    def fake_open_capture(url):
        seen["affinity_at_capture"] = set(os.sched_getaffinity(0))
        return _ClosedCapture()

    monkeypatch.setattr(worker, "open_capture", fake_open_capture)
    monkeypatch.setattr(worker, "update_status", lambda *a, **k: None)
    monkeypatch.setattr(worker, "resolve_snapshot_url", lambda cfg: None)

    def body():
        worker.camera_worker(1, "rtsp://cam/main", None, None, None, cpus)
        seen["affinity_after"] = set(os.sched_getaffinity(0))

    t = threading.Thread(target=body)
    t.start()
    t.join(timeout=30)
    assert not t.is_alive(), "нить камеры не завершилась"
    return seen


def test_thread_is_pinned_before_capture_is_opened(monkeypatch):
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip("нужно хотя бы два разрешённых ядра")
    target = [allowed[0]]

    seen = _run_camera_worker(target, monkeypatch)

    assert seen["affinity_at_capture"] == set(target), (
        "захват открыт раньше привязки — буферы канала лягут на чужую ноду")


def test_pinning_does_not_leak_to_the_whole_worker(monkeypatch):
    """Привязка канала не должна утаскивать за собой весь процесс.

    В воркере той же нитью-менеджером идут индексация сегментов,
    синхронизация слоя записи и уборка. Если бы привязка применялась к
    процессу (а `os.sched_setaffinity(0, ...)` документирован именно так,
    хотя системный вызов Linux работает по нити), первая же камера
    посадила бы весь слой записи на ядра одной ноды.
    """
    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip("нужно хотя бы два разрешённых ядра")

    _run_camera_worker([allowed[0]], monkeypatch)

    assert set(os.sched_getaffinity(0)) == set(allowed)


def test_without_layout_nothing_is_pinned(monkeypatch):
    """Односокетная машина: раскладка пуста, аффинность нити не трогается.

    Это состояние песочницы, раннеров CI и класса «малый объект» §20 —
    привязка там не даёт локальности, а планировщику мешает.
    """
    allowed = sorted(os.sched_getaffinity(0))

    seen = _run_camera_worker(None, monkeypatch)

    assert seen["affinity_at_capture"] == set(allowed)


def test_failed_pinning_does_not_stop_the_channel(monkeypatch):
    """Привязка ускоряет, а не разрешает работать.

    Ядра могут исчезнуть из разрешённых между планированием и стартом нити
    (смена cpuset, горячее отключение CPU). Канал обязан продолжить —
    отказ привязки не повод оставить камеру без аналитики.
    """
    seen = _run_camera_worker([9999], monkeypatch)

    assert "affinity_at_capture" in seen, "канал не дошёл до открытия захвата"
