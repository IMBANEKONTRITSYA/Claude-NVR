"""Конец сегмента архива: стык, обрыв, отсутствие ffprobe (D05).

Числа — с настоящего MediaMTX 1.16.1 (recordPartDuration 1 с), снятые при
воспроизведении D05: при штатной ротации mtime закрытого файла на 0.88 с
ПОЗЖЕ начала следующего; при обрыве 16 с файл с 6.133 с видео имел mtime
через 7.02 с после начала, а следующий начинался через 28 с.
Живой сценарий — в test_segment_gap_live.py; здесь — правило и его цена.
"""
import pytest

import os
import time

from record_layer import SEGMENT_JOIN_TOLERANCE_SEC, collect_complete_segments, segment_end

START = 1_793_197_794.0


class _Probe:
    def __init__(self, value):
        self.value, self.calls = value, 0

    def __call__(self, path):
        self.calls += 1
        return self.value


def test_rotation_seam_ends_at_next_start_without_probing():
    """Штатная ротация: граница — начало соседа, ffprobe не зовётся.

    Цена здесь — свойство, а не оптимизация: индексация идёт каждые ~10 с
    по всем камерам, и ffprobe на каждом сегменте каждой камеры был бы
    процессом на сегмент без всякой пользы.
    """
    probe = _Probe(24.0)
    end = segment_end(START, START + 24, mtime=START + 24.88, probe=probe, path="x.mp4")
    assert end == START + 24
    assert probe.calls == 0


def test_small_flush_lag_before_next_start_is_still_a_seam():
    probe = _Probe(None)
    end = segment_end(START, START + 24, mtime=START + 24 - SEGMENT_JOIN_TOLERANCE_SEC + 0.1,
                      probe=probe, path="x.mp4")
    assert end == START + 24, "дрожание сброса на диск не должно рисовать разрыв на каждом стыке"
    assert probe.calls == 0


def test_outage_uses_real_video_duration():
    probe = _Probe(6.133)
    end = segment_end(START, START + 28, mtime=START + 7.02, probe=probe, path="x.mp4")
    assert end == pytest.approx(START + 6.133)
    assert probe.calls == 1


def test_outage_without_ffprobe_falls_back_to_last_write():
    end = segment_end(START, START + 28, mtime=START + 7.02, probe=_Probe(None), path="x.mp4")
    assert end == pytest.approx(START + 7.02)


def test_probe_longer_than_gap_is_capped_by_next_start():
    """Испорченный ответ ffprobe не может налезть на следующий сегмент."""
    end = segment_end(START, START + 28, mtime=START + 7.02, probe=_Probe(500.0), path="x.mp4")
    assert end == START + 28


def test_last_segment_ends_at_its_video_not_beyond():
    """Последний файл камеры (соседа нет) — тоже по видео в файле."""
    end = segment_end(START, None, mtime=START + 22.19, probe=_Probe(21.2), path="x.mp4")
    assert end == pytest.approx(START + 21.2)


def test_end_never_precedes_start():
    end = segment_end(START, START + 28, mtime=START - 5, probe=_Probe(None), path="x.mp4")
    assert end == START


def _outage_archive(tmp_path, cameras: int):
    """По два сегмента на камеру с обрывом между ними — массовый обрыв."""
    for cam in range(1, cameras + 1):
        for i, start in enumerate((1_000_000, 1_000_060)):
            f = tmp_path / f"cam{cam}_{start}.mp4"
            f.write_bytes(b"x")
            # первый файл «писался» 10 с из 60 до следующего — обрыв
            os.utime(f, (start + 10, start + 10) if i == 0 else (start + 60, start + 60))
    return str(tmp_path)


def test_mass_outage_does_not_stall_the_manager_loop(tmp_path):
    """Массовый обрыв: ffprobe ограничен бюджетом прохода, а не числом камер.

    Индексация идёт в цикле менеджера. Без бюджета 40 камер × 0.1 с
    ffprobe — 4 с простоя цикла (на объекте: 14 с на 250 камерах).
    """
    seg_dir = _outage_archive(tmp_path, cameras=40)
    probe = _Probe(None)

    def slow_probe(path):
        time.sleep(0.1)
        return probe(path)

    t0 = time.monotonic()
    found = collect_complete_segments(seg_dir, now=2_000_000, probe=slow_probe, probe_budget_sec=0.5)
    elapsed = time.monotonic() - t0
    assert elapsed < 1.5, f"проход занял {elapsed:.1f} с — ffprobe не ограничен бюджетом"
    assert probe.calls < 40, "ffprobe вызван на каждый сегмент массового обрыва"
    # Сверх бюджета граница — по mtime: разрыв виден у всех камер.
    firsts = [s for s in found if s["started_ts"] == 1_000_000]
    assert len(firsts) == 40
    assert all(s["ended_ts"] == 1_000_010 for s in firsts)
