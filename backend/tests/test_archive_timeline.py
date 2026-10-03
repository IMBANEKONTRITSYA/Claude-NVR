"""Шкала архива (ТЗ §5): склейка покрытия — без БД.

Production path на настоящем Postgres проверяется в
`test_integration_archive_timeline.py`; здесь — та часть, где решается,
что считать непрерывной записью, а что дырой. Проверяется отдельно
намеренно: именно эта арифметика определяет, увидит ли оператор перерыв в
записи, и ошибка в ней не падает, а тихо рисует ровную полосу поверх
пропущенного часа.
"""
from datetime import datetime, timedelta

from app.services import archive_timeline as tl

BASE = datetime(2026, 3, 1, 0, 0, 0)


class Seg:
    """Минимальный сегмент: склейке нужны только границы."""
    def __init__(self, start_s: float, end_s: float):
        self.started_at = BASE + timedelta(seconds=start_s)
        self.ended_at = BASE + timedelta(seconds=end_s)


def test_adjacent_segments_merge_into_one_range():
    """Сегменты встык — одна непрерывная запись, а не два куска.

    Ротация файла у MediaMTX идёт каждые 5 минут; без склейки шкала суток
    показывала бы 288 разрывов там, где поток не прерывался ни разу.
    """
    ranges = tl.merge_coverage([Seg(0, 300), Seg(300, 600), Seg(600, 900)])
    assert len(ranges) == 1
    assert ranges[0].start == BASE
    assert ranges[0].duration_sec == 900


def test_small_gap_is_joined_but_real_break_is_not():
    """Граница склейки — §19 «восстановление потока ≤ 5 секунд».

    Пауза в пределах допуска — артефакт закрытия файла; всё, что дольше,
    по мерке ТЗ уже перерыв записи, и прятать его нельзя.
    """
    joined = tl.merge_coverage([Seg(0, 300), Seg(303, 600)])
    assert len(joined) == 1, "3 секунды — ротация, не перерыв"

    split = tl.merge_coverage([Seg(0, 300), Seg(320, 600)])
    assert len(split) == 2, "20 секунд без записи — дыра, её обязано быть видно"
    assert split[0].end == BASE + timedelta(seconds=300)
    assert split[1].start == BASE + timedelta(seconds=320)


def test_overlapping_and_nested_segments_do_not_duplicate_coverage():
    """Перекрытие после переоткрытия пути — один диапазон, не два.

    Слой записи, переподключившись, может записать перекрывающийся хвост;
    два диапазона поверх одного времени нарисовали бы двойную полосу и
    завысили бы «сколько записано».
    """
    ranges = tl.merge_coverage([Seg(0, 300), Seg(280, 600), Seg(400, 500)])
    assert len(ranges) == 1
    assert ranges[0].duration_sec == 600
    assert tl.recorded_seconds(ranges) == 600


def test_broken_row_with_end_before_start_does_not_stretch_range_backwards():
    """Строка с концом раньше начала — точка, а не диапазон назад."""
    ranges = tl.merge_coverage([Seg(600, 100)])
    assert ranges[0].start == ranges[0].end


def test_clamp_cuts_edges_to_the_window():
    """Крайние сегменты выходят за окно — на шкале их видно только внутри.

    Отбор идёт по пересечению, поэтому сегмент, начавшийся до окна, придёт
    целиком; без обрезки «записано за сутки» получилось бы больше суток.
    """
    ranges = tl.merge_coverage([Seg(-100, 500)])
    clamped = tl.clamp_ranges(ranges, BASE, BASE + timedelta(seconds=300))
    assert clamped[0].start == BASE
    assert clamped[0].end == BASE + timedelta(seconds=300)
    assert tl.recorded_seconds(clamped) == 300


def test_range_entirely_outside_window_disappears():
    """Диапазон, не пересекающий окно, не даёт полоски нулевой длины."""
    ranges = tl.merge_coverage([Seg(600, 900)])
    assert tl.clamp_ranges(ranges, BASE, BASE + timedelta(seconds=300)) == []


def test_empty_input_is_empty_timeline():
    assert tl.merge_coverage([]) == []
    assert tl.recorded_seconds([]) == 0


def test_query_selects_by_intersection_not_by_start():
    """Запрос отбирает пересекающие окно сегменты.

    Позитивный контроль на условие: сегмент, начавшийся до окна и
    закончившийся внутри, содержит начало запрошенного времени — фильтр
    «started_at >= date_from» потерял бы его, и шкала показала бы дыру там,
    где запись есть.
    """
    q = tl.timeline_query(1, BASE, BASE + timedelta(hours=1))
    sql = str(q.compile(compile_kwargs={"literal_binds": True}))
    assert "started_at <" in sql
    assert "ended_at >" in sql
    assert "ORDER BY video_segments.started_at" in sql
    assert "started_at >=" not in sql
