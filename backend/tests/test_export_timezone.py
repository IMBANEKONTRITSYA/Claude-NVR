"""Время в выгрузках и в расписании отчётов — в поясе объекта.

Файл CSV/Excel и письмо читают без интерфейса под рукой: столбец «Время» в
UTC без пометки на объекте в UTC+5 читается как местный и сдвигает каждое
событие на пять часов. Планировщик той же причиной слал «отчёт в 08:00» в
13:00: слот считался по часам контейнера, а они идут в UTC.
"""
from datetime import datetime, timedelta

from app.services.display_timezone import format_local, local_to_utc, utc_to_local
from app.services.report_scheduler import is_due, slot_utc
from app.services.reports import header_for

EKB = "Asia/Yekaterinburg"  # UTC+5, без перехода на летнее время


class _Sched:
    def __init__(self, period="daily", hour=8, minute=0, day_of_week=0,
                 day_of_month=1, last_sent_at=None, enabled=True):
        self.period, self.hour, self.minute = period, hour, minute
        self.day_of_week, self.day_of_month = day_of_week, day_of_month
        self.last_sent_at, self.enabled = last_sent_at, enabled


def test_export_time_is_wall_clock_of_the_site():
    assert format_local(datetime(2026, 10, 29, 10, 0, 7, 123456), EKB) == "2026-10-29 15:00:07"
    assert format_local(None, EKB) == ""


def test_invalid_zone_falls_back_to_utc():
    assert format_local(datetime(2026, 10, 29, 10, 0), "Mars/Base") == "2026-10-29 10:00:00"


def test_time_columns_name_the_zone():
    assert header_for(["ID", "Время", "Первое", "Последнее", "Камера"], EKB) == [
        "ID", f"Время ({EKB})", f"Первое ({EKB})", f"Последнее ({EKB})", "Камера"]


def test_local_utc_roundtrip_across_dst():
    for ts in (datetime(2026, 3, 29, 0, 30), datetime(2026, 10, 25, 0, 30)):
        assert local_to_utc(utc_to_local(ts, "Europe/Berlin"), "Europe/Berlin") == ts


def test_schedule_hour_is_site_time_not_process_time():
    """08:00 на объекте в UTC+5 — это 03:00 UTC, а не 08:00 UTC."""
    sched = _Sched(hour=8)
    assert slot_utc(sched, datetime(2026, 10, 29, 3, 0), EKB) == datetime(2026, 10, 29, 3, 0)
    assert is_due(sched, datetime(2026, 10, 29, 3, 0), EKB) is True
    sched.last_sent_at = slot_utc(sched, datetime(2026, 10, 29, 3, 0), EKB)
    # До сих пор слот «08:00» открывался в 08:00 UTC — на пять часов позже.
    assert is_due(sched, datetime(2026, 10, 29, 8, 0), EKB) is False
    assert is_due(sched, datetime(2026, 10, 30, 2, 59), EKB) is False
    assert is_due(sched, datetime(2026, 10, 30, 3, 0), EKB) is True


def test_site_date_decides_the_weekly_slot():
    """Понедельник 01:00 на объекте — ещё воскресенье по UTC."""
    sched = _Sched(period="weekly", hour=1, day_of_week=0)  # понедельник
    # 2026-11-02 — понедельник; 01:00 в UTC+5 = 2026-11-01 20:00 UTC (воскресенье)
    assert slot_utc(sched, datetime(2026, 11, 1, 20, 0), EKB) == datetime(2026, 11, 1, 20, 0)


def test_dst_gap_slot_is_sent_once_not_lost():
    """02:30 в ночь перевода часов вперёд не существует — отчёт всё равно уходит, один раз."""
    tz = "Europe/Berlin"
    # Вчерашний слот (28.03 02:30 CET = 01:30 UTC) уже закрыт.
    sched = _Sched(hour=2, minute=30, last_sent_at=datetime(2026, 3, 28, 1, 30))
    sent = 0
    now = datetime(2026, 3, 28, 22, 0)
    while now < datetime(2026, 3, 29, 6, 0):
        if is_due(sched, now, tz):
            sent += 1
            sched.last_sent_at = slot_utc(sched, now, tz)
            sent_at = now
        now += timedelta(minutes=1)
    assert sent == 1
    # Первый момент после несуществующих 02:30 — 03:00 CEST = 01:00 UTC.
    assert sent_at == datetime(2026, 3, 29, 1, 0)


def test_default_zone_keeps_previous_behaviour():
    sched = _Sched(hour=8)
    assert is_due(sched, datetime(2026, 8, 17, 8, 0)) is True
    assert is_due(sched, datetime(2026, 8, 17, 7, 59)) is True  # вчерашний слот не закрыт
    sched.last_sent_at = datetime(2026, 8, 16, 8, 0)
    assert is_due(sched, datetime(2026, 8, 17, 7, 59)) is False
