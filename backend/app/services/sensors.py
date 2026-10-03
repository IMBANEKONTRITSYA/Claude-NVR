"""Выбор датчика температуры для системного мониторинга (SPEC §9).

§9 перечисляет содержимое системного мониторинга поимённо: «CPU/RAM,
**температура**, место на диске». Величина бралась так:

    for entries in psutil.sensors_temperatures().values():
        if entries:
            temp = entries[0].current
            break

То есть **первая запись первой группы в порядке обхода словаря**. Порядок
этот задаётся обходом `/sys/class/hwmon` и к смыслу отношения не имеет: на
целевом сервере §20 (2× Xeon E5-2670, RAID-массив) в словаре лежат
`coretemp` с двумя сокетами, `acpitz` (корпус), `nvme` и `drivetemp` по
каждому диску массива. Что из этого окажется первым — вопрос порядка
загрузки модулей ядра, а не выбора.

Отсюда два разных способа соврать одним числом:

* **показать не тот датчик.** Подпись на «Мониторинге» — «Температура»,
  без уточнений, и читается она как температура процессора. 38 °C от
  корпусного `acpitz` при 92 °C на сокете выглядят благополучно;
* **показать не тот сокет.** У двухпроцессорной машины `coretemp` отдаёт
  `Package id 0` и `Package id 1`. `entries[0]` — это всегда первый, а
  перегревается тот, что горячее: греется обычно один (ближе к горячему
  ряду, под отказавшим вентилятором), и именно его число нужно.

Модуль — чистая функция от уже полученной карты датчиков: ни psutil, ни
чтения `/sys`. Причина ровно та же, что у `worker/record_status.py`:
решение «какой датчик показывать» обязано проверяться без нужного железа,
а нужного железа нет ни в песочнице, ни на раннере CI — `psutil.
sensors_temperatures()` возвращает там пустой словарь.
"""
from __future__ import annotations

from typing import Iterable, NamedTuple

# Группы датчиков процессора, в порядке предпочтения. Имена — это имена
# драйверов ядра: `coretemp` у Intel (в том числе у Xeon E5 целевого
# сервера), `k10temp`/`zenpower` у AMD, `cpu_thermal` у ARM-плат, на
# которых §20 допускает малые объекты.
CPU_SENSOR_GROUPS = ("coretemp", "k10temp", "zenpower", "cpu_thermal")

# Группа корпусного датчика: хуже процессорного, но лучше диска — берётся
# откатом, когда процессорных групп нет вовсе.
FALLBACK_SENSOR_GROUPS = ("acpitz", "thermal_zone0")

# Метка, по которой у `coretemp` опознаётся датчик всего сокета. Отдельные
# ядра (`Core 0`, `Core 1`, …) тоже годятся, но пакет — это то, по чему
# процессор уходит в throttling, и на него настроены пороги вендора.
PACKAGE_LABEL_PREFIX = "package id"


class Reading(NamedTuple):
    """Показание с указанием, откуда оно взято.

    Источник возвращается наружу и показывается рядом с числом
    намеренно: 38 °C от корпусного датчика и 38 °C от сокета — разные
    сведения об объекте, а подпись «Температура» без уточнения выдаёт
    второе за первое.
    """

    celsius: float
    source: str


def _entries(group: Iterable) -> list:
    """Записи группы, у которых есть осмысленное значение.

    `current` у неподключённого датчика приходит `None` или нулём; ноль
    градусов на работающем сервере — это отсутствующий датчик, а не
    показание, и максимум по группе он не портит только потому, что
    отбрасывается здесь.
    """
    out = []
    for entry in group or ():
        value = getattr(entry, "current", None)
        if value is None:
            continue
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        out.append((str(getattr(entry, "label", "") or ""), value))
    return out


def _hottest(name: str, group: Iterable) -> Reading | None:
    """Самое горячее показание группы, с предпочтением датчиков сокета.

    Максимум, а не первое и не среднее: на двухсокетной машине греется
    обычно один процессор, и среднее по двум спрячет ровно тот случай,
    ради которого температуру и смотрят.
    """
    entries = _entries(group)
    if not entries:
        return None
    packages = [(label, value) for label, value in entries
                if label.lower().startswith(PACKAGE_LABEL_PREFIX)]
    label, value = max(packages or entries, key=lambda pair: pair[1])
    return Reading(round(value, 1), f"{name}/{label}" if label else name)


def cpu_temperature(sensors: dict[str, Iterable] | None) -> Reading | None:
    """Температура для «Мониторинга» (SPEC §9) и источник, откуда она взята.

    Порядок предпочтения — от процессора к корпусу и только затем ко
    всему остальному; внутри выбранной группы берётся самое горячее
    показание.

    Диски (`nvme`, `drivetemp`) попадают лишь в последнюю очередь, когда
    других датчиков нет вовсе. Раньше они конкурировали с процессорными
    на равных — а на RAID-массиве §20 их в словаре больше всех, и шанс,
    что «Температура» покажет именно диск, был тем выше, чем крупнее
    объект.

    `None` — датчиков нет: так отвечает и Docker под Windows, и раннер
    CI. Интерфейс на `None` не рисует карточку вовсе; подставлять сюда
    ноль значило бы показать «0 °C» как показание.
    """
    if not sensors:
        return None
    for name in CPU_SENSOR_GROUPS + FALLBACK_SENSOR_GROUPS:
        reading = _hottest(name, sensors.get(name))
        if reading is not None:
            return reading
    # Ничего знакомого: берём самое горячее из всего, что есть, но
    # источник по-прежнему называем — читателю надо видеть, что число
    # пришло с диска, а не с процессора.
    best: Reading | None = None
    for name in sorted(sensors):
        reading = _hottest(name, sensors.get(name))
        if reading is not None and (best is None or reading.celsius > best.celsius):
            best = reading
    return best
