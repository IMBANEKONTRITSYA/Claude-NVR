"""Раскладка каналов аналитики по NUMA-нодам (SPEC §17).

§17 «NUMA-оптимизация (для многопроцессорных серверов)» требует привязки
воркеров к NUMA-нодам и приводит пример: «Камеры 1-60 на node 0, 61-120 на
node 1 (пример для 120 камер)». До цикла 40 в коде не было ничего: нити
камер поднимались без какой-либо аффинности, и планировщик Linux был волен
переносить канал между сокетами на каждом кванте.

**Чем это плохо именно здесь.** Канал аналитики — это несколько мегабайт
горячих буферов на кадр (кадр 720p BGR — 2.6 МБ, фон MOG2 — столько же,
тензоры детектора) плюс веса модели. На односокетной машине их размещение
безразлично. На двухсокетном сервере §20 (2× Xeon E5-2670 — тот самый
случай, ради которого §17 и написан) обращение к памяти чужой ноды идёт
через QPI: латентность примерно вдвое выше локальной, а полоса — общая на
все ядра сокета. Канал, чьи буферы лежат на node 0, а исполняется он на
node 1, платит эту разницу на каждом кадре.

**Почему привязка к ноде, а не к ядру.** Ядра внутри одной ноды делят
контроллер памяти и L3, для локальности они равноценны. Привязка к
конкретному ядру ничего не добавила бы к локальности, зато сделала бы
канал неспособным пережить соседа: пул ORT (до `MAX_THREADS_PER_CAMERA`
потоков, см. ort_threads.py) и декодер OpenCV встали бы в очередь за одно
ядро, вместо того чтобы разъехаться по свободным ядрам ноды.

**Почему одна нода — это «не привязывать вовсе».** На односокетной машине
(вся песочница, все раннеры CI, весь класс «малый объект» §20) привязка не
даёт локальности — она только отнимает у планировщика право балансировать
нагрузку. Поэтому при одной ноде план пуст, и `pin_current_thread()` не
вызывается ни разу.

**Про память.** Политика Linux по умолчанию — first-touch: страница
достаётся той ноде, чей поток к ней первым обратился. Привязка нити ДО
первого кадра означает, что все её кадровые буферы окажутся локальными.
Веса модели этим не покрываются: `FACE_APP` один на все камеры и
загружается менеджером до старта нитей, поэтому лежит на ноде менеджера.
Это осознанный размен: одна копия весов вместо копии на камеру (§16
отводит камере 0.5–2 ГБ, то есть N копий в бюджет влезли бы, но 64 канала
на целевом сервере — это десятки гигабайт ради локальности только на
чтение). Замер `perf/bench_scaling.py` показывает, чего стоит альтернатива.

Модуль намеренно живёт на одном stdlib и ничего не импортирует из
`worker.py`: так его проверяет CI-джоба `worker`, куда cv2/insightface не
ставятся.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Sequence

logger = logging.getLogger("facewatch.worker")

NODE_ROOT = "/sys/devices/system/node"

# Переменная окружения для оператора объекта: `off` выключает привязку
# совсем. Смысл — не «на всякий случай», а вполне конкретный: если
# systemd-юнит (§26) уже ограничивает сервис через `CPUAffinity=` или
# cpuset-контроллер cgroup, вторая раскладка поверх первой только запутает
# диагностику. Значение `auto` (умолчание) — привязывать, когда нод больше
# одной.
ENV_MODE = "ANALYTICS_NUMA"


def parse_cpulist(raw: str) -> list[int]:
    """Разобрать формат ядра `0-3,8,10-11` в список номеров ядер.

    Это тот же формат, в котором ядро отдаёт `cpulist` ноды и `online`.
    Диапазоны в нём могут идти вперемешку с одиночными номерами, а на
    машинах с отключёнными ядрами — быть неполными.
    """
    cpus: list[int] = []
    for part in (raw or "").strip().split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                cpus.extend(range(int(lo), int(hi) + 1))
            except ValueError:
                continue
        else:
            try:
                cpus.append(int(part))
            except ValueError:
                continue
    return sorted(set(cpus))


def _allowed_cpus() -> set[int]:
    """Ядра, на которых процессу вообще разрешено исполняться.

    Не `os.cpu_count()`: под docker-compose с `cpuset-cpus`, под systemd с
    `CPUAffinity=` (§26) и в cgroup контейнера машина видна целиком, а
    исполняться разрешено на части. Раскладка по ядрам, которых нам не
    дали, привела бы к `EINVAL` на каждой камере.
    """
    try:
        return set(os.sched_getaffinity(0))
    except (AttributeError, OSError):            # не-Linux
        return set(range(os.cpu_count() or 1))


def numa_nodes(root: str | None = None) -> list[list[int]]:
    """Ядра по NUMA-нодам, пересечённые с разрешёнными процессу.

    Возвращается список списков, отсортированный по номеру ноды. Ноды без
    единого разрешённого ядра выбрасываются — они существуют в железе, но
    для этого процесса их нет.

    `root` разрешается внутри, а не значением по умолчанию: значения
    по умолчанию вычисляются при определении функции, и подмена
    `NODE_ROOT` (тесты, нестандартный sysfs) до неё бы не доехала.
    """
    root = root or NODE_ROOT
    allowed = _allowed_cpus()
    nodes: list[tuple[int, list[int]]] = []
    try:
        entries = os.listdir(root)
    except OSError:
        # Ядро без CONFIG_NUMA или не-Linux: одна нода из всего, что дали.
        return [sorted(allowed)] if allowed else []
    for name in entries:
        if not name.startswith("node") or not name[4:].isdigit():
            continue
        try:
            with open(os.path.join(root, name, "cpulist"), encoding="ascii") as fh:
                cpus = parse_cpulist(fh.read())
        except OSError:
            continue
        usable = sorted(set(cpus) & allowed)
        if usable:
            nodes.append((int(name[4:]), usable))
    if not nodes:
        return [sorted(allowed)] if allowed else []
    nodes.sort()
    return [cpus for _, cpus in nodes]


def enabled(nodes: Sequence[Sequence[int]] | None = None,
            env: dict | None = None) -> bool:
    """Нужно ли вообще привязывать каналы на этой машине."""
    mode = (env or os.environ).get(ENV_MODE, "auto").strip().lower()
    if mode in ("off", "0", "no", "false"):
        return False
    if nodes is None:
        nodes = numa_nodes()
    return len(nodes) > 1


def plan(camera_ids: Iterable[int],
         nodes: Sequence[Sequence[int]] | None = None) -> dict[int, list[int]]:
    """Разложить камеры analytics по нодам: id камеры → разрешённые ядра.

    Раскладка блоками по отсортированным id — ровно то, что иллюстрирует
    §17 («камеры 1-60 на node 0, 61-120 на node 1»), но без единого
    зашитого числа: §22 запрещает хардкодить количество камер, поэтому
    границы блоков считаются от фактического числа каналов и фактического
    числа нод.

    Блоками, а не по кругу (`id % nodes`), намеренно. Соседние по id
    камеры на объекте — это обычно соседние физически: один этаж, один
    коммутатор, один диапазон адресов. Раскладка блоками держит такую
    группу на одном сокете, и когда оператор переводит этаж в analytics
    целиком, нагрузка ложится на одну ноду предсказуемо, а не размазывается
    по обеим. По кругу это распределение было бы столь же сбалансированным
    по числу камер, но менее предсказуемым по нагрузке.

    Пустой словарь означает «не привязывать»: одна нода, выключено
    переменной окружения или камер нет.
    """
    if nodes is None:
        nodes = numa_nodes()
    ids = sorted(set(int(c) for c in camera_ids))
    if not ids or not enabled(nodes):
        return {}
    n = len(nodes)
    total = len(ids)
    out: dict[int, list[int]] = {}
    for i, cam_id in enumerate(ids):
        # Границы блоков считаются умножением, а не делением: при total,
        # не кратном n, остаток распределяется по нодам равномерно, а не
        # сваливается целиком на последнюю (что даёт при 5 камерах и 2
        # нодах раскладку 3/2, а не 2/3 с пустым хвостом).
        node = min(i * n // total, n - 1)
        out[cam_id] = list(nodes[node])
    return out


def pin_current_thread(cpus: Sequence[int] | None) -> bool:
    """Привязать ТЕКУЩУЮ нить к набору ядер. True, если привязка встала.

    Именно нить, а не процесс. `os.sched_setaffinity(0, ...)` документирован
    как «процесс с PID pid, 0 — вызывающий процесс», но под этим лежит
    системный вызов Linux `sched_setaffinity(2)`, у которого 0 означает
    **вызывающую нить**. Проверено: после привязки нити камеры к одному
    ядру `sched_getaffinity(0)` в главной нити продолжает показывать все
    ядра. Если бы работала документированная формулировка, привязка первой
    же камеры утащила бы на её ноду весь воркер — вместе с индексацией
    сегментов и синхронизацией слоя записи.
    """
    if not cpus:
        return False
    try:
        os.sched_setaffinity(0, set(int(c) for c in cpus))
        return True
    except (AttributeError, OSError):
        # Не-Linux, либо ядра исчезли из разрешённых между планированием и
        # привязкой (горячее отключение CPU, смена cpuset). Канал при этом
        # обязан работать дальше — привязка ускоряет, а не разрешает.
        logger.warning("не удалось привязать нить камеры к ядрам",
                       extra={"cpus": sorted(cpus)}, exc_info=True)
        return False


def describe(camera_ids: Iterable[int] | None = None) -> dict:
    """Сводка раскладки для лога старта и панели «Мониторинг».

    Без неё привязка — невидимая настройка: на объекте нельзя отличить
    «сработала» от «молча не сработала», а именно это отличие и решает,
    имеет ли смысл искать причину низкого FPS в NUMA.
    """
    nodes = numa_nodes()
    mode = os.environ.get(ENV_MODE, "auto").strip().lower()
    layout = plan(camera_ids or [], nodes)
    per_node: dict[int, int] = {}
    for cam_id, cpus in layout.items():
        for idx, node_cpus in enumerate(nodes):
            if list(node_cpus) == cpus:
                per_node[idx] = per_node.get(idx, 0) + 1
                break
    return {
        "numa_nodes": len(nodes),
        "cpus_per_node": [len(c) for c in nodes],
        "mode": mode,
        "pinning": bool(layout),
        "cameras_per_node": per_node or None,
    }
