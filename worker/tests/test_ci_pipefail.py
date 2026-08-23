"""Шаг CI, отдающий вывод в `tee`, не должен скрывать падение команды.

**Класс отказа, который этот набор закрывает.** Шелл для `run:` в GitHub
Actions по умолчанию — `bash -e {0}`, **без** `pipefail`. У конвейера
`команда | tee файл` код возврата берётся от последней команды, то есть от
`tee`, а он успешен всегда. Значит любой шаг вида

    run: python perf/bench_чего_нибудь.py | tee perf-чего-нибудь.txt

отчитывается **зелёным** независимо от того, отработала команда или упала
на первой строке, — и оставляет пустой артефакт, который никто не
открывает, потому что шаг зелёный.

**Это не гипотеза.** Цикл 55 обнаружил так два молчащих замера сразу:

* `perf/bench_rto.py --hang-only` (§13, §19 RTO) — драйвер замера падал на
  `import uvicorn`, которого в джобе `perf` не стоит, а его stderr уходит
  в DEVNULL; шаг был зелёным, и отчёты циклов ссылались на его число как
  на измеренное;
* `perf/bench_model_load_stall.py` — падал на `import redis` в первый же
  свой прогон в CI, и шаг тоже был зелёным.

Лечится это одной строкой на джобу (`defaults.run.shell` с `pipefail`), но
строку легко не поставить в новой джобе — и тогда всё повторится молча.
Поэтому проверка здесь.

**Набор намеренно без PyYAML и на голом stdlib:** он обязан идти в лёгкой
джобе `worker`, где из зависимостей только pytest и несколько чистых
пакетов. Разбор построчный и опирается на отступы, которыми файл написан;
на случай, если разбор перестанет узнавать файл, стоит сторож
`test_the_parser_still_finds_the_pipelines` — без него сломавшийся разбор
дал бы пустой список и «зелёную» проверку, то есть ровно ту ошибку, против
которой весь набор.
"""
import os
import re

import pytest

WORKFLOWS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".github", "workflows",
)

# Сколько конвейеров с `tee` в наборе заведомо есть. Число заниженное: оно
# сторожит разбор, а не количество бенчмарков, и расти вместе с ними не
# обязано.
MIN_KNOWN_PIPELINES = 10


def _workflow_files() -> list[str]:
    if not os.path.isdir(WORKFLOWS):
        return []
    return [os.path.join(WORKFLOWS, n) for n in sorted(os.listdir(WORKFLOWS))
            if n.endswith((".yml", ".yaml"))]


def _pipelines() -> list[tuple[str, str, str, bool]]:
    """`(файл, джоба, строка шага, защищён ли pipefail)` по каждому `| tee`.

    Защищённым считается конвейер, у джобы которого объявлен
    `defaults.run.shell` с `pipefail`, **либо** в теле самого шага стоит
    `set -...o pipefail`. Второе — то, как защищались отдельные шаги до
    цикла 55, и оно остаётся законным.
    """
    out: list[tuple[str, str, str, bool]] = []
    for path in _workflow_files():
        lines = open(path, encoding="utf-8").read().splitlines()
        job = "<вне джобы>"
        job_indent = None
        job_has_pipefail = False
        step_has_pipefail = False
        in_steps = False
        # Разбор в два прохода: сначала для каждой джобы выясняем, есть ли
        # у неё pipefail в defaults, потом ходим по шагам. Один проход не
        # годится: `defaults` может стоять и после `steps`.
        job_pipefail: dict[str, bool] = {}
        cur = None
        for line in lines:
            m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", line)
            if m:
                cur = m.group(1)
                job_pipefail.setdefault(cur, False)
                continue
            if cur and "shell:" in line and "pipefail" in line:
                job_pipefail[cur] = True

        cur = None
        for line in lines:
            m = re.match(r"^  ([A-Za-z_][\w-]*):\s*$", line)
            if m:
                cur = m.group(1)
                step_has_pipefail = False
                continue
            if re.match(r"^      - ", line):
                step_has_pipefail = False
            if "pipefail" in line and "shell:" not in line:
                step_has_pipefail = True
            if "| tee " in line:
                protected = job_pipefail.get(cur, False) or step_has_pipefail
                out.append((os.path.basename(path), cur or "?",
                            line.strip(), protected))
    return out


def test_the_parser_still_finds_the_pipelines():
    """Сторож самого разбора.

    Без него правка отступов или переезд бенчмарков сделали бы список
    пустым, и проверка ниже прошла бы вхолостую — то есть проверка против
    ложной зелени сама стала бы ложно зелёной.
    """
    found = _pipelines()
    assert len(found) >= MIN_KNOWN_PIPELINES, (
        f"разбор нашёл только {len(found)} конвейеров с `tee` при ожидаемых "
        f"минимум {MIN_KNOWN_PIPELINES} — скорее всего сломался сам разбор, "
        "а не исчезли бенчмарки"
    )


def test_every_tee_pipeline_is_protected_by_pipefail():
    """Ни один `| tee` не должен глотать код возврата команды."""
    unprotected = [(f, j, s) for f, j, s, ok in _pipelines() if not ok]
    assert not unprotected, (
        "шаги CI, где падение команды скрыто `tee` (нет `pipefail` ни в "
        "`defaults.run.shell` джобы, ни в теле шага):\n"
        + "\n".join(f"  {f} :: джоба {j} :: {s}" for f, j, s in unprotected)
    )


@pytest.mark.parametrize("bench", ["bench_rto.py", "bench_model_load_stall.py"])
def test_benchmarks_that_were_silently_failing_are_still_wired(bench):
    """Оба замера, пойманных циклом 55, обязаны остаться в наборе.

    Проверка узкая и намеренно такая: удалить сломанный шаг — самый
    дешёвый способ «починить» красноту, и он вернул бы отсутствие замера,
    только теперь честно отсутствующего.
    """
    text = "".join(open(p, encoding="utf-8").read() for p in _workflow_files())
    assert bench in text, f"{bench} больше не запускается ни одной джобой CI"
