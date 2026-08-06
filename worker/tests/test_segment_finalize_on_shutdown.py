"""Недописанный сегмент не должен пропадать при штатной остановке (P1, цикл 23).

Продолжение полного прохода воркера по 24/7-стабильности. Закрывая
сегмент, `camera_worker()` отдаёт его в `finalize_segment()` — отдельную
нить, которая перекодирует mp4v → H.264 и **только потом** пишет строку в
`video_segments`. До этой строки записанный кусок существует лишь как
`*_tmp.mp4` на диске: в архиве его нет, отдать пользователю нечего.

Нить эта `daemon=True` и никем не отслеживалась. На остановке
`manager()` дожидался только нитей камер — а они, закрыв сегмент,
завершаются сразу. Дальше процесс выходил, интерпретатор убивал
daemon-нити, и финализация обрывалась посреди `subprocess.run(ffmpeg)`
с таймаутом 300 с.

Итог: при каждом штатном рестарте (`docker compose restart worker` —
обновление, смена настроек) терялся последний сегмент **каждой пишущей
камеры** — до минуты записи на камеру, до 16 минут на 16 камерах. Файл
оставался осиротевшим `*_tmp.mp4` и через час подчищался уборкой, то есть
следов не оставалось вовсе.

Отдельно от учёта нитей: даже дождавшись, транскод бы не успел. Grace
period `docker compose stop` по умолчанию 10 секунд, а перекодирование
минутного сегмента идёт секунды-минуты. Поэтому на остановке транскод
пропускается — файл просто переименовывается (миллисекунды), запись
сохраняется в mp4v. Это ровно тот режим деградации, который в коде уже
был для случая «транскод не удался».

Тесты идут production path: настоящие `spawn_finalize()`/
`join_finalize_threads()`/`finalize_segment()`, настоящие файлы на диске,
настоящие нити. Подменяются только запись в БД и ffmpeg — то есть внешние
границы, а не проверяемое поведение.
"""
import os
import threading
import time

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)


@pytest.fixture()
def segment(tmp_path):
    """Недописанный сегмент на диске: `*_tmp.mp4` с содержимым."""
    tmp = tmp_path / "cam7_1785000000_tmp.mp4"
    final = tmp_path / "cam7_1785000000.mp4"
    tmp.write_bytes(b"\x00" * 2048)
    return str(tmp), str(final)


@pytest.fixture()
def no_db(monkeypatch):
    """Строки `video_segments`, которые пытался записать воркер."""
    saved = []

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def add(self, row):
            saved.append(row)

        def commit(self):
            pass

    monkeypatch.setattr(worker, "Session", lambda: _FakeSession())
    return saved


@pytest.fixture(autouse=True)
def clean_shutdown_flag():
    """Флаг остановки — глобальный на модуль; возвращаем как было."""
    was_set = worker.shutdown_event.is_set()
    yield
    if not was_set:
        worker.shutdown_event.clear()


def test_pending_segment_is_finalized_before_worker_exits(segment, no_db, monkeypatch):
    """`join_finalize_threads()` дожидается незавершённой финализации.

    Без учёта нитей ожидание не за что было зацепить: `manager()` выходил,
    пока финализация ещё шла, и запись пропадала.
    """
    tmp, final = segment
    started = threading.Event()
    release = threading.Event()

    def slow_replace(src, dst):
        started.set()
        release.wait(5)
        os.rename(src, dst)

    monkeypatch.setattr(worker.os, "replace", slow_replace)
    worker.shutdown_event.set()

    now = __import__("datetime").datetime.utcnow()
    worker.spawn_finalize(7, tmp, final, now, now, "motion")
    assert started.wait(5), "финализация не стартовала — тест ничего не проверил"

    release.set()
    worker.join_finalize_threads(time.time() + 10.0)

    assert os.path.exists(final), (
        "сегмент не финализирован к моменту выхода воркера — при штатном "
        "рестарте последний кусок записи каждой камеры пропадает"
    )
    assert not os.path.exists(tmp), "остался осиротевший *_tmp.mp4"
    assert len(no_db) == 1, "строка video_segments не записана — в архиве сегмента нет"


def test_shutdown_skips_transcode(segment, no_db, monkeypatch):
    """На остановке ffmpeg не запускается: grace period его не дождётся.

    Проверяется поведение (файл сохранён, транскод не вызван), а не флаг.
    """
    tmp, final = segment
    calls = []
    monkeypatch.setattr(worker.subprocess, "run", lambda *a, **kw: calls.append(a))
    worker.shutdown_event.set()

    now = __import__("datetime").datetime.utcnow()
    worker.finalize_segment(7, tmp, final, now, now, "motion")

    assert calls == [], (
        "на остановке запущен транскод — минутный сегмент кодируется дольше "
        "grace period (10 с по умолчанию), SIGKILL оборвёт его и запись пропадёт"
    )
    assert os.path.exists(final), "запись не сохранена"
    assert len(no_db) == 1, "строка video_segments не записана"


def test_normal_operation_still_transcodes(segment, no_db, monkeypatch):
    """Контрольный: без остановки транскод выполняется как раньше.

    Без него предыдущий тест прошёл бы и на реализации, которая перестала
    перекодировать вообще — а H.264 нужен, mp4v не играется в браузере.
    """
    tmp, final = segment
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        open(final, "wb").write(b"\x00")
        return None

    monkeypatch.setattr(worker.subprocess, "run", fake_run)
    monkeypatch.setattr(worker, "_lower_priority", lambda: None)
    worker.shutdown_event.clear()

    now = __import__("datetime").datetime.utcnow()
    worker.finalize_segment(7, tmp, final, now, now, "motion")

    assert calls, "в штатном режиме транскод должен выполняться"
    assert "libx264" in calls[0] or "libx265" in calls[0]
    assert len(no_db) == 1


def test_finished_threads_do_not_accumulate(segment, no_db, monkeypatch):
    """Реестр нитей не растёт весь срок жизни процесса.

    Сегмент закрывается примерно раз в минуту на камеру; на 16 камерах
    список без уборки набирал бы десятки тысяч мёртвых объектов за сутки.
    """
    tmp, final = segment
    monkeypatch.setattr(worker, "finalize_segment", lambda *a, **kw: None)
    now = __import__("datetime").datetime.utcnow()

    for _ in range(50):
        worker.spawn_finalize(7, tmp, final, now, now, "motion").join(timeout=5)

    worker.spawn_finalize(7, tmp, final, now, now, "motion").join(timeout=5)
    assert len(worker._FINALIZE_THREADS) <= 2, (
        f"реестр нитей финализации не подчищается: {len(worker._FINALIZE_THREADS)}"
    )
