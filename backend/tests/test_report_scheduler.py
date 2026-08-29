"""Логика расписания отчётов (SPEC §8) — без БД и без почты.

Проверяется ровно то, ради чего планировщик написан руками: слот
отрабатывает один раз и не теряется при простое. Оба свойства ломаются
беззвучно — дубль выглядит как «письмо пришло дважды, наверное так надо»,
а пропуск не выглядит никак, — поэтому они и вынесены в чистые функции,
которые можно прогнать на любой дате без ожидания реального времени.
"""
from datetime import datetime, timedelta

import pytest

from app.services.report_scheduler import is_due, previous_slot


class _Sched:
    """Минимальный двойник ReportSchedule: логика слота смотрит только на
    поля расписания, ORM-объект для неё избыточен."""

    def __init__(self, period="daily", hour=8, minute=0, day_of_week=0,
                 day_of_month=1, last_sent_at=None, enabled=True):
        self.period = period
        self.hour = hour
        self.minute = minute
        self.day_of_week = day_of_week
        self.day_of_month = day_of_month
        self.last_sent_at = last_sent_at
        self.enabled = enabled


# --- ежедневное --------------------------------------------------------------

def test_daily_slot_is_today_when_time_has_passed():
    now = datetime(2026, 8, 17, 9, 30)
    assert previous_slot(_Sched(hour=8), now) == datetime(2026, 8, 17, 8, 0)


def test_daily_slot_is_yesterday_before_the_hour():
    """До 8 утра последний наступивший слот — вчерашний. Иначе отчёт ушёл
    бы в полночь, как только сменилась дата."""
    now = datetime(2026, 8, 17, 7, 59)
    assert previous_slot(_Sched(hour=8), now) == datetime(2026, 8, 16, 8, 0)


def test_daily_fires_once_per_day():
    now = datetime(2026, 8, 17, 8, 0)
    sched = _Sched(hour=8)
    assert is_due(sched, now) is True
    # Планировщик закрывает слот моментом СЛОТА
    sched.last_sent_at = previous_slot(sched, now)
    assert is_due(sched, now) is False
    # ...и следующий тик через минуту не открывает его заново
    assert is_due(sched, now + timedelta(minutes=1)) is False
    # ...а завтра — открывает
    assert is_due(sched, now + timedelta(days=1)) is True


def test_restart_within_the_slot_does_not_resend():
    """Перезапуск бэкенда в 08:00:30 не должен слать отчёт второй раз."""
    sched = _Sched(hour=8, last_sent_at=datetime(2026, 8, 17, 8, 0))
    assert is_due(sched, datetime(2026, 8, 17, 8, 0, 30)) is False


def test_downtime_across_the_slot_still_sends():
    """Бэкенд лежал с 07:50 до 08:10 — отчёт за 08:00 обязан уйти при
    старте, а не потеряться до завтра."""
    sched = _Sched(hour=8, last_sent_at=datetime(2026, 8, 16, 8, 0))
    assert is_due(sched, datetime(2026, 8, 17, 8, 10)) is True


def test_long_downtime_sends_once_not_once_per_missed_day():
    """Неделя простоя — один отчёт, а не семь писем подряд.

    Именно поэтому слот считается «назад от now», а не проходом вперёд от
    last_sent_at.
    """
    sched = _Sched(hour=8, last_sent_at=datetime(2026, 8, 10, 8, 0))
    now = datetime(2026, 8, 17, 9, 0)
    assert is_due(sched, now) is True
    sched.last_sent_at = previous_slot(sched, now)
    assert is_due(sched, now) is False


def test_never_sent_schedule_is_due():
    assert is_due(_Sched(hour=8, last_sent_at=None), datetime(2026, 8, 17, 9, 0)) is True


def test_disabled_schedule_is_never_due():
    assert is_due(_Sched(enabled=False, last_sent_at=None), datetime(2026, 8, 17, 9, 0)) is False


def test_unknown_period_is_never_due():
    """Значение из БД, записанное другой версией, не должно ронять проход
    и не должно слать отчёт «на всякий случай»."""
    sched = _Sched(period="hourly", last_sent_at=None)
    assert is_due(sched, datetime(2026, 8, 17, 9, 0)) is False


# --- еженедельное ------------------------------------------------------------

def test_weekly_slot_is_the_configured_weekday():
    # 2026-08-17 — понедельник; расписание на среду (2)
    now = datetime(2026, 8, 17, 9, 0)
    slot = previous_slot(_Sched(period="weekly", day_of_week=2, hour=8), now)
    assert slot == datetime(2026, 8, 12, 8, 0)   # прошлая среда
    assert slot.weekday() == 2


def test_weekly_on_the_day_before_the_hour_points_to_last_week():
    now = datetime(2026, 8, 12, 7, 0)            # среда, до 8 утра
    slot = previous_slot(_Sched(period="weekly", day_of_week=2, hour=8), now)
    assert slot == datetime(2026, 8, 5, 8, 0)


def test_weekly_fires_once_per_week():
    now = datetime(2026, 8, 12, 8, 0)
    sched = _Sched(period="weekly", day_of_week=2, hour=8)
    assert is_due(sched, now) is True
    sched.last_sent_at = previous_slot(sched, now)
    assert is_due(sched, now + timedelta(days=6)) is False
    assert is_due(sched, now + timedelta(days=7)) is True


# --- ежемесячное -------------------------------------------------------------

def test_monthly_slot_is_this_month_after_the_day():
    now = datetime(2026, 8, 17, 9, 0)
    assert previous_slot(_Sched(period="monthly", day_of_month=5, hour=8), now) == \
        datetime(2026, 8, 5, 8, 0)


def test_monthly_slot_rolls_back_to_previous_month():
    now = datetime(2026, 8, 3, 9, 0)
    assert previous_slot(_Sched(period="monthly", day_of_month=5, hour=8), now) == \
        datetime(2026, 7, 5, 8, 0)


def test_monthly_rollback_over_february():
    """Откат в прошлый месяц идёт через первое число, а не вычитанием 30
    суток: в феврале это дало бы не то число."""
    now = datetime(2026, 3, 2, 9, 0)
    assert previous_slot(_Sched(period="monthly", day_of_month=28, hour=8), now) == \
        datetime(2026, 2, 28, 8, 0)


def test_monthly_day_is_clamped_to_28():
    """Число больше 28 приводится к 28: расписание «31-го» молча не
    срабатывало бы в феврале, и заметили бы это через месяцы."""
    now = datetime(2026, 2, 20, 9, 0)
    slot = previous_slot(_Sched(period="monthly", day_of_month=31, hour=8), now)
    assert slot == datetime(2026, 1, 28, 8, 0)


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly"])
def test_slot_is_never_in_the_future(period):
    """Инвариант всех периодов: слот, который ещё не наступил, отправку
    открывать не должен."""
    now = datetime(2026, 8, 17, 12, 34)
    slot = previous_slot(_Sched(period=period, hour=23, minute=59), now)
    assert slot <= now
