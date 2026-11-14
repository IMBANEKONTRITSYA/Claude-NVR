"""Расписание детекции (SPEC §6: «Расписание детекции (день/ночь, рабочие
часы)»).

Все проверки — на фиксированных датах, ни одна не смотрит на «сейчас»:
тест ночного окна, написанный от текущего времени, проходил бы или падал в
зависимости от часа прогона CI (урок цикла 20 о тестах, зависящих от
времени выполнения).

Главное из проверяемого — **окно через полночь**. «Ночная охрана»
22:00–06:00 при наивной проверке `start <= t < end` не срабатывает ни одной
ночи, а система при этом выглядит полностью исправной: камера онлайн,
воркер жив, событий просто нет. Половина сценария §6 («день/ночь») ломается
беззвучно.
"""
from datetime import datetime

import pytest

from detection_schedule import MAX_WINDOWS, schedule_active

# 2026-08-17 — понедельник (weekday() == 0).
MON = "2026-08-17"
FRI = "2026-08-21"
SAT = "2026-08-22"
SUN = "2026-08-23"


def at(day: str, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00")


def sched(*windows, enabled=True):
    return {"enabled": enabled, "windows": list(windows)}


def window(start, end, days=None):
    w = {"start": start, "end": end}
    if days is not None:
        w["days"] = days
    return w


# ---------------------------------------------------------------------------
# Отсутствие расписания — детекция всегда
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("schedule", [None, {}, {"enabled": False}, {"enabled": False, "windows": []},
                                      "мусор", 42, []])
def test_no_schedule_means_detection_always_runs(schedule):
    """Расписание не задано ни у одной существующей камеры. Трактовка
    «пусто = выключено» на обновлении остановила бы аналитику на всём
    объекте без единого сообщения — поэтому пусто это «всегда».

    Сюда же — мусор вместо расписания: битое значение в колонке не должно
    молча выключать детекцию на камере навсегда.
    """
    assert schedule_active(schedule, at(MON, "03:00")) is True
    assert schedule_active(schedule, at(MON, "15:00")) is True


def test_enabled_schedule_without_windows_means_never():
    """В отличие от отсутствия расписания, это осознанное и обратимое
    состояние: администратор включил расписание и не задал ни одного окна."""
    assert schedule_active(sched(), at(MON, "12:00")) is False


# ---------------------------------------------------------------------------
# Рабочие часы
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("time_,expected", [
    ("07:59", False),
    ("08:00", True),    # граница включительно
    ("12:30", True),
    ("17:59", True),
    ("18:00", False),   # верхняя граница исключительно — иначе два соседних
                        # окна 08:00-18:00 и 18:00-22:00 перекрывались бы
    ("23:00", False),
])
def test_working_hours_window(time_, expected):
    s = sched(window("08:00", "18:00"))
    assert schedule_active(s, at(MON, time_)) is expected


def test_working_days_exclude_weekend():
    s = sched(window("08:00", "18:00", days=[0, 1, 2, 3, 4]))
    assert schedule_active(s, at(FRI, "12:00")) is True
    assert schedule_active(s, at(SAT, "12:00")) is False
    assert schedule_active(s, at(SUN, "12:00")) is False


def test_window_without_days_applies_every_day():
    """Так задаётся «ночь всегда», не перечисляя все семь дней."""
    s = sched(window("08:00", "18:00"))
    assert schedule_active(s, at(SAT, "12:00")) is True


# ---------------------------------------------------------------------------
# Окно через полночь — главное свойство
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("time_,expected", [
    ("21:59", False),
    ("22:00", True),
    ("23:59", True),
    ("00:00", True),    # ровно полночь — окно продолжается
    ("03:00", True),
    ("05:59", True),
    ("06:00", False),
    ("12:00", False),
])
def test_night_window_crosses_midnight(time_, expected):
    """Наивная проверка `start <= t < end` для 22:00–06:00 ложна всегда:
    «ночная охрана» не работала бы ни одной ночи, и выглядело бы это как
    «камера ничего не видит», а не как ошибка расписания."""
    s = sched(window("22:00", "06:00"))
    assert schedule_active(s, at(MON, time_)) is expected


def test_night_window_day_refers_to_its_start():
    """Смена, начавшаяся в пятницу вечером, продолжается в субботу ночью.
    Выключать её в полночь неверно: для дежурного это одна ночь, а не две.

    Обратная сторона: суббота 22:00 в окно «ночь с пятницы» не попадает —
    иначе `days` для ночных окон не значил бы ничего.
    """
    s = sched(window("22:00", "06:00", days=[4]))       # ночь с пятницы
    assert schedule_active(s, at(FRI, "23:00")) is True
    assert schedule_active(s, at(SAT, "03:00")) is True   # та же ночь, уже суббота
    assert schedule_active(s, at(SAT, "23:00")) is False  # следующая ночь — не пятничная
    assert schedule_active(s, at(FRI, "12:00")) is False  # днём в пятницу окна нет


def test_sunday_night_window_wraps_to_monday():
    """Граница недели: воскресенье (6) → понедельник (0). Ошибка на единицу
    в переходе через край недели даёт ровно одну неработающую ночь в семь
    дней — заметить её глазами почти невозможно."""
    s = sched(window("22:00", "06:00", days=[6]))       # ночь с воскресенья
    assert schedule_active(s, at(SUN, "23:00")) is True
    assert schedule_active(s, at(MON, "03:00")) is True
    assert schedule_active(s, at(MON, "23:00")) is False


# ---------------------------------------------------------------------------
# Несколько окон и вырожденные случаи
# ---------------------------------------------------------------------------

def test_several_windows_are_combined():
    """Рабочие часы по будням плюс ночная охрана — типовой график объекта."""
    s = sched(
        window("08:00", "18:00", days=[0, 1, 2, 3, 4]),
        window("22:00", "06:00"),
    )
    assert schedule_active(s, at(MON, "12:00")) is True    # рабочие часы
    assert schedule_active(s, at(MON, "23:00")) is True    # ночь
    assert schedule_active(s, at(MON, "19:00")) is False   # промежуток
    assert schedule_active(s, at(SAT, "12:00")) is False   # выходной день
    assert schedule_active(s, at(SAT, "23:00")) is True    # ночь и в выходной


def test_equal_start_and_end_means_whole_day():
    """00:00–00:00 — самая естественная запись для «весь день»; пустой
    интервал здесь означал бы выключенную детекцию при включённом
    расписании."""
    s = sched(window("00:00", "00:00"))
    assert schedule_active(s, at(MON, "00:00")) is True
    assert schedule_active(s, at(MON, "13:37")) is True


def test_hour_without_leading_zero_is_accepted():
    """Форма всегда пишет `HH:MM` (это же требует и схема бэкенда), но
    разбор в воркере намеренно мягче: «8:00» однозначно, а отвергнуть его —
    значит молча выключить детекцию на камере из-за ручной правки колонки."""
    s = sched({"start": "8:00", "end": "18:00"})
    assert schedule_active(s, at(MON, "12:00")) is True
    assert schedule_active(s, at(MON, "07:00")) is False


@pytest.mark.parametrize("bad", [
    {"start": "25:00", "end": "26:00"},       # часов не бывает
    {"start": "08:70", "end": "18:00"},       # минут не бывает
    {"start": "08-00", "end": "18:00"},       # не тот разделитель
    {"start": None, "end": "18:00"},
    {"start": "08:00"},                        # нет конца
    "не окно",
])
def test_broken_window_is_ignored_not_fatal(bad):
    """Значение приходит из БД, а не из проверенного на входе кода: ручная
    правка или старый формат не должны ронять нить камеры."""
    assert schedule_active(sched(bad), at(MON, "12:00")) is False


def test_broken_window_does_not_disable_the_good_ones():
    """Обратная сторона: одно битое окно не отменяет остальные — иначе
    опечатка выключала бы детекцию целиком, а не свой интервал."""
    s = sched({"start": "мусор", "end": "18:00"}, window("08:00", "18:00"))
    assert schedule_active(s, at(MON, "12:00")) is True


def test_window_count_is_capped():
    """Окна разбираются в цикле кадров: колонка не должна становиться
    способом занять воркер разбором тысячи интервалов на каждом кадре."""
    # Первые MAX_WINDOWS не совпадают со временем, подходящее окно — за
    # пределом потолка, значит учтено не будет.
    filler = [window("00:00", "00:01") for _ in range(MAX_WINDOWS)]
    s = sched(*filler, window("08:00", "18:00"))
    assert schedule_active(s, at(MON, "12:00")) is False
