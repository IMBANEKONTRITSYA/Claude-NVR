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
