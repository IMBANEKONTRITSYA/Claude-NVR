"""Состояние циклической перезаписи доезжает до §9 (SPEC §5, §9).

Пара к `test_cleanup_status_wiring.py` и заведена по тому же поводу: с
этого цикла `enforce_disk_quota()` идёт в своей нити и сторожем живости не
проверяется, поэтому её состояние обязано быть видно снаружи.

Правило показа проверяется отдельно и в джобе, которая идёт всегда
(`frontend/src/diskQuota.test.ts`); здесь — проводка: тот ли объект
обновляет проход и попадает ли его снимок в ключ `record:layer`, из
которого страницу мониторинга кормит бэкенд.

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
    monkeypatch.setattr(worker, "QUOTA_STATUS", st)
    return st


def test_a_healthy_pass_is_reported_as_done(monkeypatch, fresh_status):
    monkeypatch.setattr(worker, "enforce_disk_quota", lambda: 0)

    worker._quota_bg()

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
        return 0

    monkeypatch.setattr(worker, "enforce_disk_quota", slow)

    worker._quota_bg()

    assert seen["state"] == RUNNING


def test_a_failed_pass_is_named(monkeypatch, fresh_status):
    """Отказ перезаписи не теряет своё имя и не роняет нить.

    Отказ здесь весит больше, чем у уборки: перезапись — последнее, что не
    даёт записи встать на переполненном томе.
    """
    def boom():
        raise RuntimeError("том отвалился")

    monkeypatch.setattr(worker, "enforce_disk_quota", boom)

    worker._quota_bg()

    snap = fresh_status.snapshot()
    assert snap["state"] == FAILED
    assert snap["error"] == "enforce_disk_quota"


def test_a_skipped_pass_is_counted(monkeypatch, fresh_status):
    """Пропуск считается, хотя тревогу по нему и не поднимают.

    Тревога — нет (проход запрашивается каждые ~10 с, и любой, кто идёт
    дольше одного прохода менеджера, даёт пропуск на следующем; см.
    правило показа в diskQuota.ts). Счёт — да: он отвечает на вопрос,
    насколько перезапись отстаёт от запросов.
    """
    class _Alive:
        def is_alive(self):
            return True

    monkeypatch.setattr(worker, "_quota_thread", _Alive())

    assert worker.start_quota_pass() is False
    assert fresh_status.snapshot()["skipped"] == 1


def test_the_snapshot_travels_in_the_record_layer_payload(monkeypatch, fresh_status):
    """Ключ `record:layer` — тот самый, из которого §9 кормит бэкенд.

    Без этой строки состояние перезаписи существовало бы только внутри
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

    monkeypatch.setattr(worker, "enforce_disk_quota", lambda: 0)
    worker._quota_bg()

    payload = worker.publish_record_layer_status([])

    assert payload["quota"]["state"] == DONE
    assert payload["quota"]["last_pass_sec"] is not None


def test_quota_and_cleanup_states_do_not_share_one_object(monkeypatch):
    """Два прохода — два состояния, а не одно на двоих.

    Проверка откатом: присвойте `QUOTA_STATUS = CLEANUP_STATUS` — и упавшая
    уборка начнёт выглядеть как упавшая перезапись, то есть страница
    позовёт дежурного чинить не то.
    """
    from cleanup_status import CleanupStatus
    monkeypatch.setattr(worker, "QUOTA_STATUS", CleanupStatus())
    monkeypatch.setattr(worker, "CLEANUP_STATUS", CleanupStatus())

    def boom():
        raise RuntimeError("том отвалился")

    monkeypatch.setattr(worker, "cleanup_old", boom)
    monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})
    worker._cleanup_bg()

    assert worker.CLEANUP_STATUS.snapshot()["state"] == FAILED
    assert worker.QUOTA_STATUS.snapshot()["state"] != FAILED
