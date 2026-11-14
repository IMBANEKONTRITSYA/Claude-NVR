"""Сторож живости менеджера — класс отказа «сервис жив и не отвечает» (§13/§19).

Проверяется решение сторожа, а не его нить с реальными минутами ожидания:
`Heartbeat` принимает часы, `Watchdog.check_once()` вынесен отдельным
методом ровно ради этого. Тест, ждущий 120 секунд бюджета, в CI не живёт.

Позитивный контроль (`test_healthy_stage_does_not_trip`) обязателен: без
него «фикс», роняющий процесс на любом проходе, прошёл бы весь набор.

Только stdlib — набор гоняется лёгкой джобой CI, где нет cv2/insightface.
"""
import importlib

import pytest

import liveness as liveness_mod


class FakeClock:
    """Управляемые монотонные часы."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, sec: float) -> None:
        self.now += sec


@pytest.fixture
def liveness():
    return importlib.reload(liveness_mod)


def _hb(liveness, clock, budgets=None, default=120.0):
    return liveness.Heartbeat(budgets=budgets if budgets is not None else {"fast": 10.0, "slow": 600.0},
                              default_budget=default, clock=clock)


# --- Heartbeat -------------------------------------------------------------


def test_snapshot_reports_stage_and_elapsed(liveness):
    clock = FakeClock()
    hb = _hb(liveness, clock)
    hb.beat("fast")
    clock.advance(3.0)
    stage, elapsed, budget = hb.snapshot()
    assert stage == "fast"
    assert elapsed == pytest.approx(3.0)
    assert budget == 10.0


def test_beat_resets_elapsed(liveness):
    clock = FakeClock()
    hb = _hb(liveness, clock)
    hb.beat("fast")
    clock.advance(9.0)
    hb.beat("fast")
    assert hb.stalled() is None, "новая отметка обязана сбрасывать счётчик этапа"


def test_unknown_stage_falls_back_to_default_budget(liveness):
    clock = FakeClock()
    hb = _hb(liveness, clock, default=42.0)
    hb.beat("этап-которого-нет-в-таблице")
    clock.advance(41.0)
    assert hb.stalled() is None
    clock.advance(2.0)
    assert hb.stalled() is not None


def test_budgets_are_per_stage(liveness):
    """Общий порог пришлось бы задирать до бесполезного — в этом весь смысл."""
    clock = FakeClock()
    hb = _hb(liveness, clock)
    hb.beat("slow")
    clock.advance(120.0)
    assert hb.stalled() is None, "у долгого этапа свой бюджет"
    hb.beat("fast")
    clock.advance(11.0)
    assert hb.stalled() is not None, "у быстрого этапа тот же срок — уже зависание"


def test_stalled_reports_stage_elapsed_budget(liveness):
    clock = FakeClock()
    hb = _hb(liveness, clock)
    hb.beat("fast")
    clock.advance(11.0)
    stage, elapsed, budget = hb.stalled()
    assert (stage, budget) == ("fast", 10.0)
    assert elapsed == pytest.approx(11.0)


def test_real_budget_table_covers_manager_stages(liveness):
    """Этапы, которыми отмечается manager(), обязаны иметь свой бюджет.

    Иначе опечатка в имени этапа молча переводит его на умолчание — тот
    самый общий порог, от которого таблица и уводит.
    """
    stages = {"camera_scan", "record_layer_sync", "index_segments",
              "record_status", "motion_prune",
              "disk_alerts", "idle", "shutdown"}
    assert stages <= set(liveness.STAGE_BUDGETS_SEC)


@pytest.mark.parametrize("stage", ["model_load", "cleanup", "disk_quota"])
def test_work_that_left_the_manager_has_no_budget_here(liveness, stage):
    """У этапов, ушедших в свои нити, бюджета в таблице быть не должно.

    Обратная проверка к предыдущей: там «у каждого этапа есть бюджет»,
    здесь — «бюджета нет у того, чего менеджер не делает».

    Все три записи, пока они жили в таблице, означали одно и то же —
    санкцию на 900-секундную остановку слоя записи (SPEC §2): `model_load`
    ради загрузки модели аналитики (ушёл в цикле 55), `cleanup` ради уборки
    архива по retention (ушёл в цикле 57), `disk_quota` ради циклической
    перезаписи (ушёл в цикле 59). Возвращение любой из них сюда вернуло бы
    ту остановку молча, одной строкой.

    У `disk_quota` цена возвращения выше, чем у соседей: уборка и загрузка
    модели идут раз в час и раз за запуск, а перезапись запрашивается
    **каждым проходом менеджера** — то есть её простой не эпизод, а
    установившийся режим, пока том переполнен.
    """
    assert stage not in liveness.STAGE_BUDGETS_SEC


# --- Watchdog --------------------------------------------------------------


def _watchdog(liveness, hb, clock, grace=60.0):
    tripped = []
    wd = liveness.Watchdog(hb, kill_grace_sec=grace, clock=clock,
                           on_trip=lambda stage, elapsed: tripped.append((stage, elapsed)))
    return wd, tripped


def test_healthy_stage_does_not_trip(liveness):
    """Позитивный контроль: на здоровом воркере сторож молчит."""
    clock = FakeClock()
    hb = _hb(liveness, clock)
    wd, tripped = _watchdog(liveness, hb, clock)
    for _ in range(50):
        hb.beat("fast")
        clock.advance(1.0)
        assert wd.check_once() is False
    assert tripped == []


def test_trips_only_after_budget_plus_grace(liveness):
    clock = FakeClock()
    hb = _hb(liveness, clock)
    wd, tripped = _watchdog(liveness, hb, clock, grace=60.0)

    hb.beat("fast")
    clock.advance(11.0)          # бюджет 10 с превышен — но это ещё не убийство
    assert wd.check_once() is False
    assert tripped == []

    clock.advance(59.0)          # отсрочка 60 с ещё не вышла
    assert wd.check_once() is False
    assert tripped == []

    clock.advance(2.0)
    assert wd.check_once() is True
    assert tripped and tripped[0][0] == "fast"


def test_recovered_stage_resets_grace(liveness):
    """Рассосавшееся зависание не должно копить отсрочку между эпизодами."""
    clock = FakeClock()
    hb = _hb(liveness, clock)
    wd, tripped = _watchdog(liveness, hb, clock, grace=60.0)

    hb.beat("fast")
    clock.advance(11.0)
    wd.check_once()              # пошла отсрочка
    clock.advance(50.0)
    wd.check_once()

    hb.beat("fast")              # этап всё-таки завершился
    clock.advance(1.0)
    assert wd.check_once() is False

    hb.beat("fast")              # новый эпизод
    clock.advance(11.0)
    wd.check_once()
    clock.advance(50.0)
    assert wd.check_once() is False, (
        "отсрочка обязана отсчитываться заново, иначе второе короткое "
        "зависание убивало бы процесс мгновенно"
    )


def test_slow_stage_survives_its_whole_budget(liveness):
    """Уборка архива идёт долго законно — сторож не имеет права её оборвать."""
    clock = FakeClock()
    hb = _hb(liveness, clock)
    wd, tripped = _watchdog(liveness, hb, clock, grace=60.0)
    hb.beat("slow")
    clock.advance(599.0)
    assert wd.check_once() is False
    assert tripped == []


def test_stop_ends_the_thread(liveness):
    """Сторож обязан сниматься — иначе он срабатывает на выходящем процессе."""
    clock = FakeClock()
    hb = _hb(liveness, clock)
    wd, _ = _watchdog(liveness, hb, clock)
    wd.stop()
    wd.start()
    wd.join(timeout=3.0)
    assert not wd.is_alive()


def test_watchdog_survives_broken_heartbeat(liveness):
    """Падение сторожа тихое, поэтому он не имеет права падать.

    Если бы исключение уносило нить, класс отказа «зависание» снова не был
    бы закрыт ничем — и никто бы этого не заметил.
    """
    class Exploding:
        def stalled(self):
            raise RuntimeError("bang")

        def snapshot(self):
            raise RuntimeError("bang")

    clock = FakeClock()
    wd = liveness.Watchdog(Exploding(), kill_grace_sec=1.0, interval_sec=0.01,
                           clock=clock, on_trip=lambda *a: None)
    wd.start()
    import time as _t
    _t.sleep(0.1)
    alive = wd.is_alive()
    wd.stop()
    wd.join(timeout=3.0)
    assert alive, "нить сторожа обязана пережить исключение внутри проверки"


# --- настройки окружения ---------------------------------------------------


def test_watchdog_can_be_disabled(liveness, monkeypatch):
    monkeypatch.setenv("WORKER_WATCHDOG_ENABLED", "0")
    assert liveness.start_watchdog(_hb(liveness, FakeClock())) is None


def test_broken_env_value_falls_back_instead_of_crashing(liveness, monkeypatch):
    """Кривая переменная окружения не имеет права ронять воркер."""
    monkeypatch.setenv("WORKER_WATCHDOG_KILL_GRACE_SEC", "не-число")
    assert liveness._env_float("WORKER_WATCHDOG_KILL_GRACE_SEC", 60.0) == 60.0


def test_env_value_is_applied(liveness, monkeypatch):
    monkeypatch.setenv("WORKER_WATCHDOG_KILL_GRACE_SEC", "5")
    assert liveness._env_float("WORKER_WATCHDOG_KILL_GRACE_SEC", 60.0) == 5.0


def test_exit_code_is_distinct(liveness):
    """Рестарт сторожем обязан быть отличим от штатного выхода и от падения."""
    assert liveness.EXIT_STALLED not in (0, 1)


# --- production path: сторож действительно выходит из процесса -------------


def test_watchdog_really_exits_the_process_and_dumps_stacks():
    """Сквозная проверка: настоящий сторож, настоящий выход, настоящие стеки.

    Всё выше проверяет **решение** сторожа с подменённым `on_trip`. Само
    убийство подменить нельзя — оно и есть предмет: `os._exit()` внутри
    процесса тестов унёс бы прогон. Поэтому отдельный процесс.

    Проверяются обе половины смысла: процесс обязан выйти **с кодом 17**
    (иначе «перезапущен сторожем» не отличить от обычного падения) и
    оставить в stderr стеки нитей — на боевом сервере это единственная
    улика о том, на чём система встала, потому что после рестарта её
    больше нет.
    """
    import os
    import subprocess
    import sys

    worker_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    driver = (
        "import time\n"
        "from liveness import Heartbeat, Watchdog\n"
        "hb = Heartbeat(budgets={'wedged': 0.5}, clock=time.monotonic)\n"
        "hb.beat('wedged')\n"
        "wd = Watchdog(hb, kill_grace_sec=0.5, interval_sec=0.05)\n"
        "wd.start()\n"
        "time.sleep(30)\n"           # менеджер «завис» и больше не отмечается
        "print('SURVIVED')\n"
    )
    proc = subprocess.run([sys.executable, "-c", driver], cwd=worker_dir,
                          capture_output=True, text=True, timeout=60)

    assert proc.returncode == 17, (
        f"сторож обязан выйти с кодом 17, а вышел с {proc.returncode}; "
        f"stdout={proc.stdout!r}"
    )
    assert "SURVIVED" not in proc.stdout, "процесс пережил зависание"
    # Стеки: faulthandler пишет их сырым текстом в stderr.
    assert "Thread" in proc.stderr or "File " in proc.stderr, (
        "в stderr обязаны быть стеки нитей — без них на боевом сервере "
        f"причина зависания теряется навсегда; stderr={proc.stderr[:400]!r}"
    )
