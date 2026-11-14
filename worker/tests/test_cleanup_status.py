"""Состояние проходов уборки архива для §9 — worker/cleanup_status.py.

Проверяется правило, по которому §9 отличает исправную уборку от
сломанной. Тесты идут на голом stdlib и в **лёгкой** джобе CI: тесты
самой уборки поднимают настоящий `manager()` и потому уходят в
`pytest.importorskip("worker")`, то есть в CI молча скипаются. Решение о
том, звать дежурного или молчать, проверяться при этом обязано всегда.
"""
import pytest

from cleanup_status import DONE, FAILED, IDLE, RUNNING, CleanupStatus


class FakeClock:
    """Управляемое время: длительность прохода — часть проверяемого ответа."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, sec):
        self.now += sec


def test_before_the_first_pass_the_state_is_meaningful():
    """§9 обязан получить осмысленный ответ и до первой уборки.

    Свежезапущенный воркер — не авария: `idle` без длительности прошлого
    прохода отличается от «проход упал», и интерфейс не должен объявлять
    второе по первому.
    """
    st = CleanupStatus(clock=FakeClock())
    snap = st.snapshot()
    assert snap["state"] == IDLE
    assert snap["last_pass_sec"] is None
    assert snap["error"] is None
    assert snap["skipped"] == 0


def test_a_running_pass_reports_how_long_it_has_been_running():
    clock = FakeClock()
    st = CleanupStatus(clock=clock)
    st.pass_started()
    clock.advance(41.4)
    snap = st.snapshot()
    assert snap["state"] == RUNNING
    assert snap["seconds"] == pytest.approx(41.4)
    # Длительность прошлого прохода не подменяется длительностью текущего.
    assert snap["last_pass_sec"] is None


def test_a_finished_pass_reports_its_duration_not_the_time_since():
    """Секунды и длительность прохода — разные величины, и обе нужны.

    `seconds` после завершения — «сколько прошло с конца уборки»,
    `last_pass_sec` — «сколько она шла». Путаница между ними давала бы на
    странице «уборка шла 3 часа» через три часа после нормального прохода.
    """
    clock = FakeClock()
    st = CleanupStatus(clock=clock)
    st.pass_started()
    clock.advance(30.0)
    st.pass_finished()
    clock.advance(600.0)
    snap = st.snapshot()
    assert snap["state"] == DONE
    assert snap["last_pass_sec"] == pytest.approx(30.0)
    assert snap["seconds"] == pytest.approx(600.0)
    assert snap["error"] is None


def test_a_failed_stage_names_itself():
    """Имя упавшего этапа — единственное, что отличает «что чинить»."""
    clock = FakeClock()
    st = CleanupStatus(clock=clock)
    st.pass_started()
    clock.advance(5.0)
    st.pass_finished(["cleanup_old"])
    snap = st.snapshot()
    assert snap["state"] == FAILED
    assert snap["error"] == "cleanup_old"


def test_both_stages_failing_names_both():
    st = CleanupStatus(clock=FakeClock())
    st.pass_started()
    st.pass_finished(["cleanup_old", "prune_orphan_media"])
    assert st.snapshot()["error"] == "cleanup_old, prune_orphan_media"


def test_a_healthy_pass_after_a_failed_one_clears_the_error():
    """Иначе авария висела бы на странице до перезапуска воркера."""
    st = CleanupStatus(clock=FakeClock())
    st.pass_started()
    st.pass_finished(["cleanup_old"])
    assert st.snapshot()["state"] == FAILED
    st.pass_started()
    st.pass_finished()
    snap = st.snapshot()
    assert snap["state"] == DONE
    assert snap["error"] is None


def test_skipped_passes_accumulate_and_survive_a_healthy_pass():
    """Пропуски не обнуляются удавшимся проходом — и это осознанно.

    Один уложившийся проход не отменяет того, что архив в целом чистится
    медленнее, чем растёт: диск от этого не перестал заполняться. Счётчик
    держится до перезапуска воркера, чтобы признак не мигал.
    """
    st = CleanupStatus(clock=FakeClock())
    assert st.pass_skipped() == 1
    assert st.pass_skipped() == 2
    st.pass_started()
    st.pass_finished()
    assert st.snapshot()["skipped"] == 2


def test_the_clock_is_monotonic_by_default():
    """Перевод системных часов не должен «омолаживать» идущий проход.

    Тот же довод, что в `liveness.Heartbeat`: на объекте без интернета
    первая синхронизация ntp двигает системное время скачком, и проход,
    идущий полчаса, показал бы отрицательную длительность.
    """
    import time
    st = CleanupStatus()
    assert st._clock is time.monotonic
