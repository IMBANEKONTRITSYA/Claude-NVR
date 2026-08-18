"""Замер §19 «восстановление потока ≤ 5 секунд после обрыва»
(`perf/bench_recovery.py`).

Сам замер поднимает настоящий MediaMTX, публикует RTSP-источник, рвёт его
и ждёт, пока запись на диске снова вырастет, — минуты на прогон и внешний
бинарник. Проверять здесь надо не это, а две вещи, которые определяют,
что именно измерено:

* **вердикт выносится по худшему прогону.** Результат зависит от фазы
  цикла повторных подключений MediaMTX, то есть размазан по интервалу;
  сведение по среднему или медиане отвечало бы на вопрос «сколько
  обычно», а §19 ограничивает худший случай — тот, что определяет,
  сколько архива теряется на обрыве;
* **конфигурация пути берётся из production-функции**, а не переписана в
  замере. Реконнект — поведение MediaMTX, и зависит оно ровно от того,
  что задаёт `worker/record_layer.py:path_conf()` (в первую очередь
  `sourceOnDemand: no`). Копия конфигурации молча разошлась бы с боевой,
  и замер отчитывался бы о поведении, которого на объекте нет. Ровно этот
  класс ошибки цикл 26 нашёл в бенчмарке поиска по архиву, а цикл 39 — в
  замере remux.
"""
import sys
from pathlib import Path

import pytest

PERF = Path(__file__).resolve().parents[2] / "perf"


def _module():
    if str(PERF) not in sys.path:
        sys.path.insert(0, str(PERF))
    import bench_recovery  # noqa: PLC0415
    return bench_recovery


def _run(outage: float, recovery: float) -> dict:
    return {"outage_sec": outage, "recovery_sec": recovery,
            "within_budget": recovery <= _module().RECOVERY_BUDGET_SEC}


def test_budget_is_the_spec_number():
    """§19: «Восстановление потока ≤ 5 секунд после обрыва»."""
    assert _module().RECOVERY_BUDGET_SEC == 5.0


def test_verdict_is_taken_from_the_worst_run_not_the_typical_one():
    """Один прогон за бюджетом делает вердикт fail, даже если остальные внутри.

    Замер цикла 40 дал ровно такой набор (3.57 … 6.61 с при неизменном
    коде): по медиане он выглядел бы выполненным, а по худшему — нет.
    Выполнен он не был.
    """
    runs = [_run(2, 3.57), _run(3, 3.60), _run(5, 6.61)]
    out = _module().summarize(runs)

    assert out["worst_recovery_sec"] == 6.61
    assert out["best_recovery_sec"] == 3.57
    assert out["verdict"] == "fail"


def test_all_runs_inside_the_budget_pass():
    """Позитивный контроль: вердикт не прибит к fail."""
    out = _module().summarize([_run(2, 1.2), _run(30, 4.9)])
    assert out["verdict"] == "pass"


def test_exactly_at_the_budget_is_still_pass():
    """Граница включительно: §19 говорит «≤ 5 секунд»."""
    assert _module().summarize([_run(2, 5.0)])["verdict"] == "pass"


def test_no_runs_is_not_a_pass():
    """Отсутствие замера — не выполнение норматива.

    Пустой набор получается, когда прогон упал или был пропущен; вердикт
    `pass` на нём означал бы, что норматив закрывается ничем.
    """
    out = _module().summarize([])
    assert out["verdict"] == "fail"
    assert out["worst_recovery_sec"] is None


def test_path_config_comes_from_the_worker_not_from_a_copy():
    """Замер конфигурирует путь production-функцией слоя записи.

    Проверяется не текст, а результат: конфигурация обязана нести
    `sourceOnDemand: False` — именно она заставляет MediaMTX держать
    соединение с камерой и переподключаться самому, то есть создаёт то
    поведение, длительность которого замер и меряет. С `sourceOnDemand:
    True` сервер вообще не подключается, пока никто не смотрит, и замер
    показывал бы не восстановление записи, а скорость открытия потока по
    запросу.
    """
    mod = _module()
    conf = mod._production_path_conf("rtsp://127.0.0.1:8554/src")

    assert conf["sourceOnDemand"] is False
    assert conf["source"] == "rtsp://127.0.0.1:8554/src"
    assert conf["record"] is True
    # Каталог замера отведён отдельно от архива песочницы.
    assert conf["recordPath"].startswith(mod.SEGDIR)


def test_missing_mediamtx_is_a_skip_not_a_failure():
    """Без бинарника замер сообщает о пропуске, а не падает.

    Прогон в лёгком окружении (джоба `backend`, локальный pytest) не
    должен выглядеть провалом норматива: провал и отсутствие замера — это
    разные сообщения, и путать их нельзя.
    """
    mod = _module()
    saved = mod.MEDIAMTX_BIN
    try:
        mod.MEDIAMTX_BIN = "/nonexistent/mediamtx"
        out = mod.run([2])
    finally:
        mod.MEDIAMTX_BIN = saved
    assert "skipped" in out
    assert "verdict" not in out


@pytest.mark.parametrize("outage", [2, 5, 15, 30])
def test_default_outages_probe_both_sides_of_the_budget(outage):
    """Набор длительностей обрыва перекрывает бюджет с обеих сторон.

    §2 и §13 описывают «автопереподключение с экспоненциальной
    задержкой». Если задержка действительно растёт, то короткий обрыв
    укладывается в норматив, а длинный — нет, и одно число про «обрыв»
    ничего не значило бы. Набор обязан содержать длительности и короче
    бюджета, и заметно длиннее его — иначе замер по построению не смог бы
    увидеть рост.
    """
    defaults = _module().DEFAULT_OUTAGES
    assert outage in defaults
    assert min(defaults) < _module().RECOVERY_BUDGET_SEC < max(defaults)
