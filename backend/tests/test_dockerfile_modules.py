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
Docker, но проверяет именно тот инвариант, что и `docker build` в проде.

**Цикл 21: проверка распространена на upscaler/.** Она была написана под
воркер и знала только о нём, а `upscaler/Dockerfile` нёс ровно тот же
поимённый `COPY upscaler.py .` — то есть та же мина под тем же взрывателем,
просто ещё не наступили: до цикла 21 апскейл был одним файлом без единого
локального импорта. Цикл 21 добавил ему `logging_utils.py`, и без
одновременной правки Dockerfile образ упал бы в CrashLoop при первом
`docker compose up`. Проверка теперь параметризована по сервисам, так что
следующий сервис с локальными модулями достаточно дописать в SERVICES.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# (каталог сервиса, имя модуля-точки входа). Точка входа — то, что
# запускает CMD Dockerfile'а; от неё обход идёт по локальным импортам.
SERVICES = [
    ("worker", "worker"),
    ("upscaler", "upscaler"),
]

# Модули, о существовании которых проверка знает на сегодня, — страховка
# от того, что сам обходчик импортов сломается молча и начнёт возвращать
# пустое множество (тогда проверка ниже пройдёт на любом Dockerfile).
KNOWN_MODULES = {
    "worker": {
        "worker", "backoff", "shutdown", "logging_utils", "hwaccel",
        "onvif_client", "onvif_api", "embed_api", "record_encode",
    },
    "upscaler": {"upscaler", "logging_utils"},
}


def _local_module_names(service_dir: Path, entrypoint: str) -> set[str]:
    """Все <service>/*.py файлы, из которых точка входа (транзитивно)
    что-то импортирует локально — то, что обязано попасть в образ."""
    names = {entrypoint}
    seen: set[str] = set()
    stack = [entrypoint]
    while stack:
        mod = stack.pop()
        if mod in seen:
            continue
        seen.add(mod)
        path = service_dir / f"{mod}.py"
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # \s* в начале строки — worker.py делает и обычные top-level
        # импорты, и отложенные внутри функций (например, embed_api
        # импортируется лениво, только когда включён embed-API), оба вида
        # обязаны попасть в образ одинаково.
        for m in re.finditer(r"^\s*from (\w+) import|^\s*import (\w+)\b", text, re.MULTILINE):
            candidate = m.group(1) or m.group(2)
            if (service_dir / f"{candidate}.py").is_file():
                names.add(candidate)
                stack.append(candidate)
    return names


@pytest.mark.parametrize("service,entrypoint", SERVICES)
def test_local_module_walker_finds_known_modules(service, entrypoint):
    found = _local_module_names(ROOT / service, entrypoint)
    for expected in KNOWN_MODULES[service]:
        assert expected in found, (
            f"обходчик локальных импортов не нашёл {service}/{expected}.py — "
            "проверка ниже не защитит от регрессии, если он сломан"
        )


@pytest.mark.parametrize("service,entrypoint", SERVICES)
def test_dockerfile_copies_every_locally_imported_module(service, entrypoint):
    service_dir = ROOT / service
    dockerfile = (service_dir / "Dockerfile").read_text(encoding="utf-8")
    required = _local_module_names(service_dir, entrypoint)

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
        f"{service}/Dockerfile не копирует в образ: {sorted(missing)} — "
        f"{entrypoint}.py упадёт с ModuleNotFoundError при старте контейнера "
        "(поимённый список COPY разошёлся с реальными зависимостями; "
        "проще перейти на `COPY *.py ./`, см. комментарий в Dockerfile)"
    )
