"""Порог §2/§15 применяется к настоящему захвату, а не только в чистой функции.

`test_analytics_source.py` проверяет решение (какой поток выбрать по
измеренному разрешению). Здесь проверяется, что `camera_worker()` это
решение действительно исполняет: измеряет открытый захват, переоткрывается
на основном потоке, когда субпоток не дотянул, и что все последующие
переоткрытия (реконнект, возврат из окна расписания §6) идут уже по
выбранному адресу.

Разделение не формальное. До цикла 53 `analyze_url = sub_rtsp_url or
rtsp_url` стояло одной строкой, и никакая проверка решения не поймала бы
ошибку в том, каким адресом реально открывается поток.

Требует полный requirements.txt воркера (импорт `worker.py` тянет cv2).
"""
import os

import pytest

import analytics_source

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

MAIN = "rtsp://cam/main"
SUB = "rtsp://cam/sub"


class _Capture:
    """Открытый захват с заданным разрешением.

    `width`/`height` = None означает «свойство отдаёт 0» — сборка OpenCV
    без него либо поток с неразобранными заголовками; тогда
    capture_resolution() обязана перейти на первый кадр.
    """

    def __init__(self, width=None, height=None, frame_size=None, opened=True):
        self._w, self._h = width, height
        self._frame_size = frame_size
        self._opened = opened
        self.released = False
        self.reads = 0

    def isOpened(self):
        return self._opened

    def get(self, prop):
        import cv2
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._w or 0)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._h or 0)
        return 0.0

    def read(self):
        self.reads += 1
        if not self._frame_size:
            return False, None
        import numpy as np
        w, h = self._frame_size
        return True, np.zeros((h, w, 3), dtype="uint8")

    def release(self):
        self.released = True


def _capture_factory(monkeypatch, by_url):
    """Подменяет open_capture; возвращает журнал открытых адресов."""
    opened: list[str] = []

    def fake_open_capture(url):
        opened.append(url)
        return by_url[url]

    monkeypatch.setattr(worker, "open_capture", fake_open_capture)
    return opened


class TestCaptureResolution:
    def test_reads_properties_without_decoding_a_frame(self, monkeypatch):
        cap = _Capture(width=704, height=576)
        assert worker.capture_resolution(cap) == (704, 576)
        assert cap.reads == 0, "разрешение из свойств не должно стоить кадра"

    def test_falls_back_to_first_frame(self, monkeypatch):
        """Свойство отдало 0 — берём кадр: он и есть то, что увидит детектор."""
        cap = _Capture(width=0, height=0, frame_size=(352, 288))
        assert worker.capture_resolution(cap) == (352, 288)
        assert cap.reads == 1

    def test_unmeasurable_returns_none(self):
        cap = _Capture(width=0, height=0, frame_size=None)
        assert worker.capture_resolution(cap) == (None, None)

    def test_broken_capture_does_not_raise(self):
        """Замер — вспомогательная операция и падать на ней нельзя."""
        class Broken:
            def get(self, prop):
                raise RuntimeError("сборка без свойства")

            def read(self):
                raise RuntimeError("поток умер")

        assert worker.capture_resolution(Broken()) == (None, None)


class TestOpenAnalyticsCapture:
    def test_low_res_sub_is_reopened_on_main(self, monkeypatch):
        """CIF-субпоток: захват переоткрывается на основном потоке."""
        sub_cap = _Capture(width=352, height=288)
        main_cap = _Capture(width=1920, height=1080)
        opened = _capture_factory(monkeypatch, {SUB: sub_cap, MAIN: main_cap})

        cap, decision = worker.open_analytics_capture(1, MAIN, SUB)

        assert opened == [SUB, MAIN], "субпоток открывается первым — его и меряем"
        assert cap is main_cap
        assert sub_cap.released, "проваленный субпоток обязан быть отпущен"
        assert decision["url"] == MAIN
        assert decision["stream"] == "main"
        assert decision["reason"] == analytics_source.SUB_BELOW_FLOOR
        assert (decision["width"], decision["height"]) == (352, 288)

    def test_good_sub_is_kept_and_opened_once(self, monkeypatch):
        sub_cap = _Capture(width=704, height=576)
        opened = _capture_factory(monkeypatch, {SUB: sub_cap})

        cap, decision = worker.open_analytics_capture(1, MAIN, SUB)

        assert opened == [SUB], "лишнее открытие основного потока стоит соединения"
        assert cap is sub_cap
        assert not sub_cap.released
        assert decision["reason"] == analytics_source.SUB_MEETS_FLOOR

    def test_no_sub_opens_main_without_probing(self, monkeypatch):
        main_cap = _Capture(width=1920, height=1080)
        opened = _capture_factory(monkeypatch, {MAIN: main_cap})

        cap, decision = worker.open_analytics_capture(1, MAIN, None)

        assert opened == [MAIN]
        assert main_cap.reads == 0, "мерить нечего: основной поток допустим всегда"
        assert decision["reason"] == analytics_source.NO_SUB

    def test_unmeasurable_sub_is_left_alone(self, monkeypatch):
        """Не измерили — не платим за декод основного потока вслепую."""
        sub_cap = _Capture(width=0, height=0, frame_size=None)
        opened = _capture_factory(monkeypatch, {SUB: sub_cap, MAIN: _Capture()})

        cap, decision = worker.open_analytics_capture(1, MAIN, SUB)

        assert opened == [SUB]
        assert cap is sub_cap
        assert decision["reason"] == analytics_source.SUB_RESOLUTION_UNKNOWN

    def test_closed_capture_is_returned_for_backoff(self, monkeypatch):
        """Поток не открылся — решать не по чему, уходим на обычный backoff."""
        sub_cap = _Capture(opened=False)
        opened = _capture_factory(monkeypatch, {SUB: sub_cap})

        cap, decision = worker.open_analytics_capture(1, MAIN, SUB)

        assert opened == [SUB]
        assert not cap.isOpened()
        assert decision["url"] == SUB, "неоткрывшийся субпоток не повод сменить схему"


class _FixedDatetime:
    """`datetime` воркера с фиксированным «сейчас» — как в
    `test_detection_schedule_gating.py`. Расписание «по текущему времени»
    делало бы тест зависимым от часа прогона CI."""

    def __init__(self, iso: str):
        from datetime import datetime as _dt
        self._dt = _dt
        self.value = iso

    def now(self, tz=None):
        return self._dt.fromisoformat(self.value)

    def __getattr__(self, name):
        from datetime import datetime as _dt
        return getattr(_dt, name)


# Полдень против ночного окна: нить уходит в паузу по расписанию §6 и
# начинает часто опрашивать состояние камеры — самый дешёвый способ
# довести настоящую `camera_worker()` до штатного выхода.
NIGHT_ONLY = {"enabled": True, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6],
                                            "start": "22:00", "end": "06:00"}]}


class TestCameraWorkerUsesDecision:
    def _harness(self, monkeypatch, captures_by_url, schedules):
        opened = _capture_factory(monkeypatch, captures_by_url)
        published: dict = {}
        dropped: list = []
        monkeypatch.setattr(worker, "publish_analytics_source",
                            lambda cam_id, d: published.update({cam_id: d}))
        monkeypatch.setattr(worker, "drop_analytics_source", dropped.append)
        monkeypatch.setattr(worker, "update_status", lambda *a, **k: None)
        monkeypatch.setattr(worker, "resolve_snapshot_url", lambda cfg: None)
        monkeypatch.setattr(worker, "save_latest_frame", lambda *a, **k: None)
        monkeypatch.setattr(worker, "SCHEDULE_POLL_SEC", 0.01)
        monkeypatch.setattr(worker, "datetime", _FixedDatetime("2026-08-17T12:00:00"))

        calls = {"n": 0}

        def state(cam_id):
            i = calls["n"]
            calls["n"] += 1
            if i >= len(schedules):
                return None, False, None, None      # камера отключена — выход
            return None, True, None, schedules[i]

        monkeypatch.setattr(worker, "load_cam_state", state)
        return opened, published, dropped

    def test_low_res_sub_moves_the_thread_to_main(self, monkeypatch):
        """Настоящая `camera_worker()` работает по основному потоку и
        сообщает об этом в §9."""
        sub_cap = _Capture(width=352, height=288)
        main_cap = _Capture(width=1280, height=720, frame_size=(1280, 720))
        opened, published, _ = self._harness(
            monkeypatch, {SUB: sub_cap, MAIN: main_cap}, [NIGHT_ONLY, NIGHT_ONLY])

        worker.camera_worker(7, MAIN, None, SUB, None, None)

        assert opened[:2] == [SUB, MAIN], "субпоток измерен, затем брошен"
        assert published[7]["stream"] == "main"
        assert published[7]["reason"] == analytics_source.SUB_BELOW_FLOOR
        assert (published[7]["width"], published[7]["height"]) == (352, 288)

    def test_good_sub_is_reported_as_sub(self, monkeypatch):
        """Обратная сторона: без неё «фикс», уводящий на основной поток
        всегда, выглядел бы исправным."""
        sub_cap = _Capture(width=704, height=576, frame_size=(704, 576))
        opened, published, _ = self._harness(
            monkeypatch, {SUB: sub_cap}, [NIGHT_ONLY, NIGHT_ONLY])

        worker.camera_worker(7, MAIN, None, SUB, None, None)

        assert opened == [SUB], "основной поток не открывался"
        assert published[7]["stream"] == "sub"
        assert published[7]["reason"] == analytics_source.SUB_MEETS_FLOOR

    def test_record_is_dropped_when_thread_stops(self, monkeypatch):
        """Запись о потоке не должна пережить нить: иначе §9 показывает
        поток аналитики на камере, которая не обрабатывается."""
        sub_cap = _Capture(width=704, height=576, frame_size=(704, 576))
        _, _, dropped = self._harness(
            monkeypatch, {SUB: sub_cap}, [NIGHT_ONLY, NIGHT_ONLY])

        worker.camera_worker(7, MAIN, None, SUB, None, None)

        assert 7 in dropped

    def test_unopenable_stream_drops_a_stale_record(self, monkeypatch):
        """Нить не поднялась вовсе — выход идёт мимо общего finally.

        `manager()` пересоздаёт нить каждые ~10 с; без снятия записи камера
        с неоткрывающимся потоком навсегда осталась бы в §9 с тем потоком,
        что был у неё в прошлый удачный запуск.
        """
        sub_cap = _Capture(opened=False)
        _, published, dropped = self._harness(
            monkeypatch, {SUB: sub_cap}, [NIGHT_ONLY])

        worker.camera_worker(7, MAIN, None, SUB, None, None)

        assert dropped == [7]
        assert 7 not in published, (
            "решение, принятое без единого прочитанного кадра, описывает "
            "намерение, а не работающую аналитику"
        )
