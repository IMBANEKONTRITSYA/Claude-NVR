"""Окна наблюдения и отбор сегментов без движения (SPEC §6).

Чистая логика режима «запись только при движении»: что трекер накопит в
цикле кадров и какие сегменты уборка сочтёт пустыми. Проверяется без БД и
без OpenCV, поэтому идёт в лёгкой CI-джобе воркера.

Главный класс ошибки, ради которого написан этот файл: механизм удаляет
архив, и «движения не было» обязано отличаться от «аналитика не
смотрела». Половина проверок ниже — про второе.
"""
from datetime import datetime, timedelta

from motion_windows import (MAX_GAP_SEC, MAX_WINDOW_SEC, MotionWindowTracker,
                            segments_without_motion)

T0 = datetime(2026, 8, 17, 12, 0, 0)


class Seg:
    def __init__(self, camera_id, start_min, end_min):
        self.camera_id = camera_id
        self.started_at = T0 + timedelta(minutes=start_min)
        self.ended_at = T0 + timedelta(minutes=end_min)

    def __repr__(self):  # чтобы отчёт pytest читался
        return f"Seg(cam={self.camera_id}, {self.started_at:%H:%M}-{self.ended_at:%H:%M})"


class Win:
    def __init__(self, camera_id, start_min, end_min, motion):
        self.camera_id = camera_id
        self.started_at = T0 + timedelta(minutes=start_min)
        self.ended_at = T0 + timedelta(minutes=end_min)
        self.motion = motion


# --- трекер -----------------------------------------------------------


def feed(tracker, start, stop, step=5.0, motion=False):
    """Прогнать наблюдения через трекер с шагом `step` секунд.

    Шаг обязан быть меньше MAX_GAP_SEC: реальные кадры приходят не реже
    idle_fps (2 в секунду даже в полном покое), и «наблюдение раз в
    полминуты» трекер справедливо считает разрывом. Первая же редакция
    этих тестов подавала кадры через 30 с и падала — не на коде, а на
    собственной невнимательности.
    """
    out = []
    ts = start
    while ts <= stop:
        out += tracker.observe(ts, motion)
        ts += step
    return out


def test_tracker_emits_window_after_max_length():
    t = MotionWindowTracker()
    # Внутри окна ничего не отдаётся: нить камеры не должна ходить в БД
    # на каждый кадр.
    assert feed(t, 1000.0, 1000.0 + MAX_WINDOW_SEC - 5.0) == []
    out = feed(t, 1000.0 + MAX_WINDOW_SEC, 1000.0 + MAX_WINDOW_SEC)
    assert len(out) == 1
    assert out[0].started_ts == 1000.0
    assert out[0].ended_ts == 1000.0 + MAX_WINDOW_SEC
    assert out[0].motion is False


def test_single_motion_frame_marks_whole_window():
    """Человек, мелькнувший в одном кадре, сохраняет всю минуту."""
    t = MotionWindowTracker()
    t.observe(0.0, False)
    t.observe(5.0, True)
    out = feed(t, 10.0, MAX_WINDOW_SEC)
    assert len(out) == 1 and out[0].motion is True


def test_gap_closes_window_at_last_observation():
    """Разрыв наблюдения не заносится в покрытие.

    Окно обязано закрыться ПРЕДЫДУЩИМ наблюдением: промежуток между ним
    и следующим кадром никто не видел, и включить его в покрытие значило
    бы разрешить удаление записи за время простоя аналитики.
    """
    t = MotionWindowTracker()
    t.observe(0.0, False)
    t.observe(1.0, False)
    out = t.observe(1.0 + MAX_GAP_SEC + 1, False)
    assert len(out) == 1
    assert out[0].ended_ts == 1.0


def test_new_window_starts_seamlessly_after_full_window():
    """После полного окна следующее начинается тем же кадром, без зазора."""
    t = MotionWindowTracker()
    first = feed(t, 0.0, MAX_WINDOW_SEC)[0]
    second = feed(t, MAX_WINDOW_SEC + 5.0, 2 * MAX_WINDOW_SEC)[0]
    assert second.started_ts == first.ended_ts


def test_close_flushes_open_window():
    t = MotionWindowTracker()
    t.observe(0.0, True)
    t.observe(3.0, False)
    out = t.close()
    assert len(out) == 1 and out[0].ended_ts == 3.0 and out[0].motion is True
    # Повторный close ничего не дублирует: нить может закрыться дважды
    # (штатный выход + finally).
    assert t.close() == []


def test_close_on_untouched_tracker_is_empty():
    assert MotionWindowTracker().close() == []


# --- отбор сегментов --------------------------------------------------


def test_covered_and_quiet_segment_is_pruned():
    seg = Seg(1, 0, 10)
    windows = [Win(1, -1, 5, False), Win(1, 5, 11, False)]
    assert segments_without_motion([seg], windows, guard_sec=0) == [seg]


def test_segment_with_motion_survives():
    seg = Seg(1, 0, 10)
    windows = [Win(1, -1, 5, False), Win(1, 5, 11, True)]
    assert segments_without_motion([seg], windows, guard_sec=0) == []


def test_uncovered_segment_survives():
    """Аналитика не работала — сегмент остаётся, что бы ни было в кадре."""
    seg = Seg(1, 0, 10)
    assert segments_without_motion([seg], [], guard_sec=0) == []


def test_partially_covered_segment_survives():
    """Просмотрена половина сегмента — удалять нельзя.

    Иначе одной секунды наблюдения хватало бы, чтобы снести десять минут
    записи.
    """
    seg = Seg(1, 0, 10)
    windows = [Win(1, 0, 4, False)]
    assert segments_without_motion([seg], windows, guard_sec=0) == []


def test_coverage_hole_in_the_middle_survives():
    """Дыра внутри сегмента (перезапуск воркера) — сегмент остаётся."""
    seg = Seg(1, 0, 10)
    # Зазор между окнами — 3 минуты, много больше MAX_GAP_SEC.
    windows = [Win(1, 0, 4, False), Win(1, 7, 11, False)]
    assert segments_without_motion([seg], windows, guard_sec=0) == []


def test_adjacent_windows_join_across_frame_interval():
    """Соседние окна сливаются: зазор в кадр — не дыра в наблюдении."""
    seg = Seg(1, 0, 10)
    gap = timedelta(seconds=MAX_GAP_SEC / 2)
    w1 = Win(1, 0, 5, False)
    w2 = Win(1, 5, 11, False)
    w2.started_at = w1.ended_at + gap
    windows = [w1, w2]
    # Границы сегмента подвинуты внутрь покрытия, чтобы проверялось
    # именно слияние, а не края.
    seg.started_at = w1.started_at
    assert segments_without_motion([seg], windows, guard_sec=0) == [seg]


def test_guard_keeps_segment_next_to_motion():
    """Запас до и после движения (pre/post record).

    Движение началось через полминуты после конца сегмента — сегмент
    остаётся: человек входит в кадр раньше, чем срабатывает детектор.
    """
    seg = Seg(1, 0, 10)
    windows = [Win(1, 0, 11, False),
               Win(1, 10, 12, True)]
    windows[1].started_at = seg.ended_at + timedelta(seconds=30)
    assert segments_without_motion([seg], windows, guard_sec=60) == []
    # Тот же набор с меньшим запасом — сегмент уходит: проверка о запасе,
    # а не о том, что «что-то мешает удалению».
    assert segments_without_motion([seg], windows, guard_sec=5) == [seg]


def test_windows_of_other_camera_do_not_count():
    """Покрытие соседней камеры не разрешает удалять чужой архив."""
    seg = Seg(1, 0, 10)
    windows = [Win(2, -1, 11, False)]
    assert segments_without_motion([seg], windows, guard_sec=0) == []


def test_motion_of_other_camera_does_not_save_segment():
    seg = Seg(1, 0, 10)
    windows = [Win(1, -1, 11, False), Win(2, 0, 10, True)]
    assert segments_without_motion([seg], windows, guard_sec=0) == [seg]


def test_mixed_batch_splits_correctly():
    """Пакет из нескольких камер и сегментов разбирается по одному."""
    quiet = Seg(1, 0, 10)
    busy = Seg(1, 10, 20)
    unseen = Seg(2, 0, 10)
    windows = [Win(1, -1, 21, False), Win(1, 12, 13, True)]
    out = segments_without_motion([quiet, busy, unseen], windows, guard_sec=0)
    assert out == [quiet]
