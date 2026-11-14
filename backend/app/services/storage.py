"""Расчёт хранения архива (SPEC §5, §21).

Чистые функции без БД и файловой системы: прогноз и калькулятор считаются
над числами, поэтому проверяются арифметически, а не прогоном на 36 ТБ.

Формулы намеренно продублированы с `worker/storage.py` — это тот же
случай, что и с тремя копиями `logging_utils`: у бэкенда и воркера разные
контексты сборки Docker-образов и разные наборы зависимостей, общего
пакета между ними нет. Дублируются при этом только формулы (три строки);
решения об удалении живут исключительно в воркере, и здесь их нет.
Расхождение стережёт `tests/test_storage_forecast.py`, который сверяет
обе копии с числами из SPEC §21 напрямую.
"""
from __future__ import annotations

import os

BYTES_PER_GB = 1024 ** 3

# SPEC §21: «Формула расчёта: Mbps × 10.8 = GB/сутки на камеру».
GB_PER_DAY_PER_MBPS = 10.8

# SPEC §14: «Алерты: диск > 80%/90%».
DISK_WARN_PCT = 80.0
DISK_CRIT_PCT = 90.0


def nominal_gb_per_day(bitrate_kbps: float, cameras: int = 1) -> float:
    """Номинальный расход по формуле SPEC §21."""
    return max(0.0, bitrate_kbps) / 1000.0 * GB_PER_DAY_PER_MBPS * max(0, cameras)


def required_gb(bitrate_kbps: float, cameras: int, days: int) -> float:
    """Калькулятор хранения SPEC §21: битрейт × камеры × дни → объём."""
    return nominal_gb_per_day(bitrate_kbps, cameras) * max(0, days)


def days_left(free_bytes: int, bytes_per_day: float) -> float | None:
    """«На сколько дней хватит места» (SPEC §5).

    `None` на неизмеренном расходе: «бесконечность» в первые минуты после
    старта — уверенное враньё, а прочерк администратор читает правильно.
    """
    if bytes_per_day <= 0:
        return None
    return max(0.0, free_bytes / bytes_per_day)


def disk_alert_level(used_pct: float, warn_pct: float = DISK_WARN_PCT,
                     crit_pct: float = DISK_CRIT_PCT) -> str | None:
    """`None` | `"warning"` | `"critical"` по заполнению диска (SPEC §14)."""
    if used_pct >= crit_pct:
        return "critical"
    if used_pct >= warn_pct:
        return "warning"
    return None


def calibration(measured_gb_per_day: float, nominal_gb_per_day_value: float) -> float | None:
    """Отношение фактического расхода к номинальному (SPEC §21, калибровка).

    SPEC: «после 24 ч работы система сравнивает номинальный и фактический
    расход и показывает скорректированный прогноз». Здесь считается только
    коэффициент; решение, доверять ли ему, принимает вызывающий по объёму
    накопленных данных — на паре сегментов он ничего не значит.

    `None`, когда номинал неизвестен (нет включённых камер): делить на ноль
    и отдавать «коэффициент 0» значило бы показать «расход в 0 раз больше
    расчётного» вместо честного прочерка.
    """
    if nominal_gb_per_day_value <= 0:
        return None
    return measured_gb_per_day / nominal_gb_per_day_value


# Сколько часов записи (по часам, от начала первого сегмента окна до конца
# последнего) нужно, чтобы прогноз считался по факту, а не по номиналу.
MIN_MEASURED_SPAN_SEC = 600.0


def measured_bytes_per_day(bytes_recorded: int, span_sec: float) -> float | None:
    """Суточный расход архива по тому, что действительно записано.

    DEPLOY_FINDINGS D07 (2026-10-28): после нескольких минут записи прогноз
    обещал «сотни суток». Расход брался как СУММА байт за последние сутки —
    даже когда запись шла 30 минут, то есть занижался во столько раз,
    во сколько сутки длиннее фактически записанного (≈ 48× за полчаса).

    Здесь — байт за время, которое запись действительно покрывает (от начала
    первого сегмента окна до конца последнего), приведённое к суткам.
    Совокупно по всем камерам и без умножения на их число: камера, которая
    включена, но не пишет, расхода не создаёт и прогноз не завышает. None —
    записи меньше `MIN_MEASURED_SPAN_SEC`, и честнее показать номинал.
    """
    if span_sec < MIN_MEASURED_SPAN_SEC or bytes_recorded <= 0:
        return None
    return bytes_recorded / min(span_sec, 86400.0) * 86400.0


def wsl_virtual_disk_note(path: str, proc_version: str = "/proc/version",
                          proc_mounts: str = "/proc/mounts") -> str | None:
    """Предупреждение, если архив лежит на виртуальном диске WSL2.

    D07: в Docker Desktop на Windows именованный том живёт внутри
    виртуального диска WSL2, и statvfs отдаёт его номинальную ёмкость
    (на объекте — ~1 ТБ «свободно» при 378 ГБ на единственном диске C:).
    Изнутри контейнера настоящий предел не узнать; распознать ситуацию —
    можно: ядро WSL2 и файловая система ext4 у каталога архива (каталог
    Windows, смонтированный в контейнер, был бы 9p/drvfs и показывал бы
    настоящее место). Production — .deb на Linux (§26), там этого нет.
    """
    try:
        with open(proc_version, encoding="utf-8", errors="replace") as fh:
            if "microsoft" not in fh.read().lower():
                return None
        best, fstype = "", ""
        target = os.path.realpath(path)
        with open(proc_mounts, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mnt = parts[1].replace("\\040", " ")
                if (target == mnt or target.startswith(mnt.rstrip("/") + "/")) and len(mnt) > len(best):
                    best, fstype = mnt, parts[2]
    except OSError:
        return None
    if fstype in ("9p", "drvfs", "fuse.grpcfuse", "virtiofs"):
        return None
    return ("Архив лежит на виртуальном диске WSL2 (Docker Desktop): показана его номинальная ёмкость, "
            "а настоящий предел — свободное место на диске Windows, где лежит образ WSL. Прогноз "
            "дней ниже может быть завышен; для объекта — пакет .deb на Linux с отдельным диском архива.")
