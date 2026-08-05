"""Регрессия: worker/Dockerfile перечислял копируемые .py-модули поимённо
(`COPY worker.py embed_api.py backoff.py shutdown.py ./`). Каждый раз, когда
из worker.py выделяли новый модуль (record_encode.py, hwaccel.py,
onvif_client.py, onvif_api.py, logging_utils.py — все по очереди), про
обновление этой строки забывали. Результат — образ воркера собирался
успешно (pip install отрабатывал), но контейнер падал в CrashLoop с
`ModuleNotFoundError` при первом же импорте в worker.py, потому что нужный
файл просто не попадал в образ.

Ни один из ~17 предыдущих циклов аудита это не поймал: песочница без
Docker-демона гоняла только `pytest` с моками отдельных модулей, а не
настоящую сборку образа — статическая проверка ниже дешева и не требует
Docker, но проверяет именно тот инвариант, что и `docker build` в проде."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKER_DIR = ROOT / "worker"
DOCKERFILE = WORKER_DIR / "Dockerfile"


def _local_module_names():
    """Все worker/*.py файлы, из которых worker.py (транзитивно) что-то
    импортирует локально — то, что обязано попасть в образ."""
    names = {"worker"}
    seen = set()
    stack = ["worker"]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = WORKER_DIR / f"{mod}.py"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # \s* в начале строки — worker.py делает и обычные top-level
        # импорты, и отложенные внутри функций (например, embed_api
        # импортируется лениво, только когда включён embed-API), оба вида
        # обязаны попасть в образ одинаково.
        for m in re.finditer(r"^\s*from (\w+) import|^\s*import (\w+)\b", text, re.MULTILINE):
            candidate = m.group(1) or m.group(2)
            if (WORKER_DIR / f"{candidate}.py").is_file():
                names.add(candidate)
                stack.append(candidate)
    return names


def test_all_local_worker_modules_present_on_disk_are_tracked():
    # Страховка от того, что сам обходчик выше сломается молча: должен
    # найти хотя бы известные на сегодня локальные модули воркера.
    found = _local_module_names()
    for expected in {
        "worker", "backoff", "shutdown", "logging_utils", "hwaccel",
        "onvif_client", "onvif_api", "embed_api", "record_encode",
    }:
        assert expected in found, (
            f"обходчик локальных импортов не нашёл {expected}.py — "
            "проверка ниже не защитит от регрессии, если он сломан"
        )


def test_dockerfile_copies_every_locally_imported_worker_module():
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    required = _local_module_names()

    if re.search(r"^COPY\s+\*\.py\s", dockerfile, re.MULTILINE):
        # Глоб по всем .py в контексте сборки — новый модуль подхватывается
        # автоматически, специально перечислять нечего.
        return

    copied = set()
    for m in re.finditer(r"^COPY\s+(.+?)\s+\S+\s*$", dockerfile, re.MULTILINE):
        for token in m.group(1).split():
            if token.endswith(".py"):
                copied.add(token[:-3])

    missing = {f"{name}.py" for name in required if name not in copied}
    assert not missing, (
        f"worker/Dockerfile не копирует в образ: {sorted(missing)} — "
        "worker.py упадёт с ModuleNotFoundError при старте контейнера "
        "(поимённый список COPY разошёлся с реальными зависимостями; "
        "проще перейти на `COPY *.py ./`, см. комментарий в Dockerfile)"
    )
