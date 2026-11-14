"""Состояние уборки архива доезжает от нити уборки до §9 (SPEC §5, §9).

Правило показа проверяется отдельно и в лёгкой джобе
(`test_cleanup_status.py`); здесь — проводка: тот ли объект обновляет
проход уборки и попадает ли его снимок в ключ `record:layer`, из которого
страницу мониторинга кормит бэкенд.

Требует полный requirements.txt воркера — как и остальные тесты, которым
нужен настоящий `worker` (урок цикла 16).
"""
import os

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

from cleanup_status import DONE, FAILED, RUNNING  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_status(monkeypatch):
    """Своё состояние на каждый тест: счётчик пропусков живёт до перезапуска."""
    from cleanup_status import CleanupStatus
    st = CleanupStatus()
    monkeypatch.setattr(worker, "CLEANUP_STATUS", st)
    return st


def test_a_healthy_pass_is_reported_as_done(monkeypatch, fresh_status):
    monkeypatch.setattr(worker, "cleanup_old", lambda: None)
    monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})

    worker._cleanup_bg()

    snap = fresh_status.snapshot()
    assert snap["state"] == DONE
    assert snap["error"] is None
    assert snap["last_pass_sec"] is not None


def test_the_pass_is_marked_running_while_it_runs(monkeypatch, fresh_status):
    """Снимок обязан показывать `running` изнутри прохода, а не после него.

    Это и есть случай, ради которого поле заведено: том встал, проход не
    кончается, и страница должна показать «идёт N», а не «idle».
    """
    seen = {}

    def slow():
        seen["state"] = fresh_status.snapshot()["state"]

    monkeypatch.setattr(worker, "cleanup_old", slow)
    monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})

    worker._cleanup_bg()

    assert seen["state"] == RUNNING


def test_a_failed_stage_is_named_and_the_other_still_runs(monkeypatch, fresh_status):
    """Отказ retention не отменяет уборку сирот и не теряет своё имя."""
    ran = []

    def boom():
        raise RuntimeError("том отвалился")

    monkeypatch.setattr(worker, "cleanup_old", boom)
    monkeypatch.setattr(worker, "prune_orphan_media", lambda: ran.append(1) or {})

    worker._cleanup_bg()

    assert ran == [1], "второй этап обязан отработать после отказа первого"
    snap = fresh_status.snapshot()
    assert snap["state"] == FAILED
    assert snap["error"] == "cleanup_old"


def test_a_skipped_pass_is_counted(monkeypatch, fresh_status):
    """Отказ стартовать поверх идущего прохода виден в §9, а не только в логе."""
    class _Alive:
        def is_alive(self):
            return True

    monkeypatch.setattr(worker, "_cleanup_thread", _Alive())

    assert worker.start_cleanup_pass() is False
    assert fresh_status.snapshot()["skipped"] == 1


def test_the_snapshot_travels_in_the_record_layer_payload(monkeypatch, fresh_status):
    """Ключ `record:layer` — тот самый, из которого §9 кормит бэкенд.

    Без этой строки состояние уборки существовало бы только внутри
    процесса воркера, то есть нигде.
    """
    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            return {}

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segments", lambda ids: {})
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)

    monkeypatch.setattr(worker, "cleanup_old", lambda: None)
    monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})
    worker._cleanup_bg()

    payload = worker.publish_record_layer_status([])

    assert payload["cleanup"]["state"] == DONE
    assert payload["cleanup"]["last_pass_sec"] is not None
