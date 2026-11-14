"""Пауза нити камеры вне окна расписания детекции (SPEC §6).

Чистая функция расписания проверяется в `test_detection_schedule.py`; здесь
— то, что делает с ней нить камеры, и два свойства, которые ломаются молча:

1. **Вне окна захват отпускается.** В этом весь смысл функции на целевом
   сервере: `cap.read()` в цикле кадров не throttl'ится, декодирование идёт
   постоянно и не зависит от частоты детекции. «Детекция только ночью» без
   освобождения захвата экономила бы детектор, но не декод — то есть
   заметно меньше, чем обещает.

2. **Статус камеры при этом не меняется на offline.** §9 требует алерта на
   потерю потока; выставь здесь «offline» — и дежурный получал бы ложную
   тревогу каждый вечер по расписанию. Камера в это время действительно
   онлайн: слой записи продолжает её писать, пауза касается только
   аналитики (§2 — слои независимы).

Проверяется поведение настоящего `camera_worker()`; подменяются только
границы процесса — захват RTSP, статус в БД, состояние камеры.
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

import numpy as np  # noqa: E402

NIGHT_ONLY = {"enabled": True, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6],
                                            "start": "22:00", "end": "06:00"}]}
ALWAYS = None


# Субпоток камеры этого набора — D1 PAL. §1 называет «CIF-D1» типовым
# субпотоком, а §2/§15 требуют от источника кадров аналитики разрешения не
# ниже 640×480: 704×576 — верхний край этого диапазона, порог проходит.
# Числа здесь не декорация: `camera_worker()` меряет открытый захват и
# уводит аналитику на основной поток, если субпоток порогу не отвечает
# (worker/analytics_source.py). Набор проверяет расписание §6, поэтому
# камера ему нужна заведомо исправная — иначе половина проверок ловила бы
# не паузу по расписанию, а переключение потока.
SUB_WIDTH, SUB_HEIGHT = 704, 576


class _Capture:
    def __init__(self):
        self.released = False
        self.reads = 0

    def isOpened(self):
        return True

    def get(self, prop_id):
        # Разрешение отдаётся по своим идентификаторам, а не одним числом
        # на любое свойство: захват, сообщающий «25» в ответ на запрос
        # ширины кадра, не бывает, и подменять им настоящий значит
        # проверять поведение на входе, которого не существует.
        import cv2
        if prop_id == cv2.CAP_PROP_FRAME_WIDTH:
            return float(SUB_WIDTH)
        if prop_id == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(SUB_HEIGHT)
        return 25.0

    def read(self):
        self.reads += 1
        return True, np.zeros((SUB_HEIGHT, SUB_WIDTH, 3), dtype=np.uint8)

    def release(self):
        self.released = True


@pytest.fixture()
def harness(monkeypatch):
    """Захват, статусы и состояние камеры; сам цикл кадров — настоящий."""
    captures: list[_Capture] = []
    statuses: list[str] = []

    def open_capture(url):
        cap = _Capture()
        captures.append(cap)
        return cap

    monkeypatch.setattr(worker, "open_capture", open_capture)
    monkeypatch.setattr(worker, "update_status", lambda cam_id, status, **kw: statuses.append(status))
    monkeypatch.setattr(worker, "save_latest_frame", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "resolve_snapshot_url", lambda *a, **kw: None)
    # Иначе спящая камера ждала бы полминуты на каждой итерации.
    monkeypatch.setattr(worker, "SCHEDULE_POLL_SEC", 0.01)
    return captures, statuses


def _state_sequence(monkeypatch, schedules, captures=None):
    """`load_cam_state`, отдающая расписания по списку; после исчерпания —
    камера отключена, и нить штатно завершается.

    Попутно снимает состояние захвата НА МОМЕНТ каждого вызова — то есть
    внутри работающей нити, а не после её выхода. Без этого проверка «вне
    окна захват отпущен» была бы ложно-зелёной: общий `finally` отпускает
    захват на любом выходе, поэтому `released` к концу теста истинно и на
    коде, который во время паузы ничего не отпускает.
    """
    calls = {"n": 0, "released_at_call": []}

    def state(cam_id):
        i = calls["n"]
        calls["n"] += 1
        if captures is not None:
            calls["released_at_call"].append(
                captures[-1].released if captures else None)
        if i >= len(schedules):
            return None, False, None, None      # камера отключена — выход
        return None, True, None, schedules[i]

    monkeypatch.setattr(worker, "load_cam_state", state)
    return calls


def test_outside_window_releases_the_capture(harness, monkeypatch):
    """Вне окна декодирование должно прекращаться, а не только детекция.

    Смотреть на `released` после выхода нити бессмысленно: общий `finally`
    отпускает захват на любом пути, и проверка проходила бы на коде, где
    паузы нет вовсе. Поэтому состояние снимается изнутри — на втором
    обращении к `load_cam_state`, то есть уже в цикле паузы.
    """
    captures, _ = harness
    calls = _state_sequence(monkeypatch, [NIGHT_ONLY, NIGHT_ONLY], captures)
    # Полдень: ночное окно закрыто.
    monkeypatch.setattr(worker, "datetime", _FixedDatetime("2026-08-17T12:00:00"))

    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url="rtsp://cam/sub")

    assert captures, "захват не открывался — тест ничего не проверил"
    assert len(calls["released_at_call"]) >= 2, (
        "нить не дошла до второго чтения состояния — пауза не выполнялась, "
        "и тест ничего не проверил"
    )
    assert calls["released_at_call"][1] is True, (
        "во время паузы захват остался открытым: декодирование продолжается, "
        "и экономия сводится к одному детектору"
    )
    assert captures[0].reads == 0, (
        "вне окна из потока читались кадры — декодирование не остановлено"
    )


def test_pause_does_not_report_camera_offline(harness, monkeypatch):
    """Ложная тревога «потеря потока» (§9) каждый вечер — цена ошибки."""
    _, statuses = harness
    _state_sequence(monkeypatch, [NIGHT_ONLY, NIGHT_ONLY])
    monkeypatch.setattr(worker, "datetime", _FixedDatetime("2026-08-17T12:00:00"))

    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url="rtsp://cam/sub")

    assert "offline" not in statuses, (
        f"пауза по расписанию выставила статус offline ({statuses}) — "
        "дежурный получит алерт о потере потока по каждой камере вечером"
    )


def test_inside_window_camera_is_processed(harness, monkeypatch):
    """Обратная сторона: внутри окна нить работает как обычно. Без этой
    проверки «фикс», выключающий детекцию всегда, выглядел бы исправным."""
    captures, _ = harness
    _state_sequence(monkeypatch, [NIGHT_ONLY])
    monkeypatch.setattr(worker, "datetime", _FixedDatetime("2026-08-17T23:00:00"))

    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url="rtsp://cam/sub")

    assert captures[0].reads > 0, "внутри окна кадры не читались"
    # Переоткрытий быть не должно: окно открыто с самого начала, и лишний
    # цикл release/open означал бы, что пауза срабатывает внутри окна.
    assert len(captures) == 1


def test_camera_without_schedule_is_processed(harness, monkeypatch):
    """Расписания нет ни у одной существующей камеры: обновление не должно
    останавливать аналитику на объекте."""
    captures, _ = harness
    _state_sequence(monkeypatch, [ALWAYS])
    monkeypatch.setattr(worker, "datetime", _FixedDatetime("2026-08-17T12:00:00"))

    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url="rtsp://cam/sub")

    assert captures[0].reads > 0


def test_window_opening_reopens_the_capture(harness, monkeypatch):
    """Открытие окна должно возобновлять аналитику само, без перезапуска
    воркера: иначе «детекция с 22:00» означала бы «с ближайшего рестарта»."""
    captures, _ = harness
    _state_sequence(monkeypatch, [NIGHT_ONLY, NIGHT_ONLY])
    clock = _FixedDatetime("2026-08-17T12:00:00")
    monkeypatch.setattr(worker, "datetime", clock)
    # Второе чтение состояния приходится уже на открытое окно.
    clock.advance_after = 1
    clock.next_value = "2026-08-17T23:00:00"

    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url="rtsp://cam/sub")

    assert len(captures) >= 2, (
        "захват не переоткрыт после открытия окна — детекция не возобновится "
        "до перезапуска воркера"
    )
    assert captures[-1].reads > 0


class _FixedDatetime:
    """Замена `datetime` в модуле воркера: `now()` отдаёт фиксированный
    момент. Проверять расписание «по текущему времени» нельзя — тест
    проходил бы или падал в зависимости от часа прогона CI."""

    def __init__(self, iso: str):
        from datetime import datetime as _dt
        self._dt = _dt
        self.value = iso
        self.next_value: str | None = None
        self.advance_after: int | None = None
        self.calls = 0

    def now(self, tz=None):
        self.calls += 1
        if (self.advance_after is not None and self.next_value
                and self.calls > self.advance_after):
            self.value = self.next_value
        return self._dt.fromisoformat(self.value)

    def __getattr__(self, name):
        # Остальной модуль пользуется datetime.utcnow()/fromtimestamp() и т.д.
        from datetime import datetime as _dt
        return getattr(_dt, name)
