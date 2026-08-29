"""Сведение повторов `perf/bench_load.py` к худшему и арифметика памяти.

Зачем это тестом, а не прогоном замера. Сам замер поднимает MediaMTX, N
публикующих ffmpeg и каналы аналитики с моделью — минуты и внешние
зависимости. Сведение результатов и пересчёт удельной памяти — чистые
функции над словарями; проверять их прогоном значило бы не проверять
вовсе (тот же довод, что в `test_bench_chain_median.py`).

Зачем «по худшему». Carryover цикла 44: «число, полученное одним
прогоном, — не результат замера, а его выборка». Отчёт того цикла
опубликовал 0.21 с там, где худший случай был 2.42 с, и обнаружилось это
на первом повторном прогоне. У `bench_load.py` потолок §19 до цикла 46
проверялся по единственному прогону — то есть ровно так же.

Тонкость, ради которой тест и написан: худшее берётся **по каждой
величине отдельно**, а не «худшим прогоном целиком». Один прогон может
дать пик CPU, другой — просадку темпа архива; норматив, нарушенный хоть
в одном, нарушен.
"""
import sys
from pathlib import Path

import pytest

PERF = Path(__file__).resolve().parents[2] / "perf"


def _mod():
    if str(PERF) not in sys.path:
        sys.path.insert(0, str(PERF))
    if not (PERF / "bench_load.py").is_file():  # pragma: no cover
        pytest.skip("perf/bench_load.py отсутствует")
    try:
        import bench_load
    except ImportError as exc:
        # Отсутствующая зависимость (psutil) — законный пропуск. Любая
        # другая ошибка импорта означает, что замер сломан, и молча
        # пропустить её значило бы получить зелёный прогон на неработающем
        # файле: ровно этот сценарий всплыл на верификации откатом.
        if "psutil" in str(exc):
            pytest.skip(f"нет зависимости замера: {exc}")
        raise
    return bench_load


def _verdict(**over):
    base = {
        "machine_cpu_pct": 40.0,
        "machine_ram_pct": 40.0,
        "machine_ram_used_mb": 4000.0,
        "archive_rate_delta_pct": 0.0,
        "record_cores_per_camera_delta_pct": 0.0,
        "analytics_fps_per_channel": 8.0,
        "record_rss_mb_per_camera": 10.0,
        "analytics_rss_mb_per_channel": 300.0,
        "meets_cpu_budget": True,
        "meets_ram_budget": True,
        "layers_independent": True,
        "analytics_meets_target": True,
        "segments_kept_growing": True,
        "record_ram_bracket_mb": [50.0, 100.0],
        "analytics_ram_bracket_mb": [500.0, 2048.0],
    }
    base.update(over)
    return base


def test_single_run_is_returned_as_is():
    m = _mod()
    v = _verdict()
    assert m.worst_verdict([v]) == v


def test_worst_is_taken_per_value_not_per_run():
    """Главный инвариант: пик CPU из одного прогона и просадка архива из
    другого обязаны попасть в один вердикт."""
    m = _mod()
    # Чистый прогон намеренно ПОСЛЕДНИЙ: сведение начинается с копии
    # последнего вердикта, и на порядке [плохой, плохой, чистый] видно,
    # что худшее действительно вытаскивается, а не достаётся даром от
    # того, что последним оказался нужный прогон. На первой редакции
    # этого теста чистый прогон стоял первым — и верификация откатом
    # показала, что тест переживает удаление половины сведения.
    out = m.worst_verdict([
        _verdict(machine_cpu_pct=95.0),          # тут вылез CPU
        _verdict(archive_rate_delta_pct=-30.0),  # тут просел архив
        _verdict(machine_ram_pct=91.0),          # тут память
        _verdict(),                              # а этот прогон чистый
    ])
    assert out["machine_cpu_pct"] == 95.0
    assert out["archive_rate_delta_pct"] == -30.0
    assert out["machine_ram_pct"] == 91.0
    assert out["runs"] == 4


def test_boolean_verdicts_are_recomputed_from_the_worst_numbers():
    """Иначе «худшее» осталось бы только в таблице: булев вердикт пришёл
    бы из последнего прогона и сказал бы, что норматив выполнен."""
    m = _mod()
    out = m.worst_verdict([_verdict(machine_cpu_pct=95.0), _verdict()])
    assert out["machine_cpu_pct"] == 95.0
    assert out["meets_cpu_budget"] is False

    out = m.worst_verdict([_verdict(machine_ram_pct=91.0), _verdict()])
    assert out["meets_ram_budget"] is False

    out = m.worst_verdict([_verdict(archive_rate_delta_pct=-12.0), _verdict()])
    assert out["layers_independent"] is False

    out = m.worst_verdict([_verdict(analytics_fps_per_channel=3.1), _verdict()])
    assert out["analytics_meets_target"] is False

    out = m.worst_verdict([_verdict(segments_kept_growing=False), _verdict()])
    assert out["segments_kept_growing"] is False


def test_budgets_are_the_spec_numbers():
    """§19: «CPU ≤ 80 % при полной нагрузке (запас 20 %)», «RAM ≤ 80 % от
    доступной (запас 20 %)»."""
    m = _mod()
    assert m.CPU_BUDGET_PCT == 80.0
    assert m.RAM_BUDGET_PCT == 80.0
    # §16: «RAM: ~50-100 MB на камеру (буферы)» для слоя записи,
    # «~500 MB-2 GB на камеру (модель + буферы)» для аналитики.
    assert m.REC_RAM_MB_PER_CAMERA == (50.0, 100.0)
    assert m.ANALYTICS_RAM_MB_PER_CHANNEL == (500.0, 2048.0)


def test_analytics_ram_is_counted_as_growth_between_phases():
    """Память слоя аналитики — прирост RSS процесса замера между фазами.
    В базовой фазе он уже держит интерпретатор, psutil и прочитанный клип;
    записать это в стоимость канала значило бы приписать аналитике чужое.
    """
    m = _mod()
    base = {"machine_cpu_pct": 20.0, "machine_ram_pct": 30.0,
            "machine_ram_used_mb": 3000.0, "analytics_rss_mb": 200.0,
            "archive_mb_per_sec": 1.0, "record_cores_per_camera": 0.01,
            "record_rss_mb_per_camera": 9.0, "segments_written": 4}
    combined = dict(base, analytics_rss_mb=1000.0, machine_cpu_pct=50.0,
                    machine_ram_pct=55.0, analytics={"fps_per_channel": 8.0,
                                                     "meets_target": True})
    v = m._verdict(base, combined, cameras=8, analytics=2)
    # (1000 − 200) / 2 канала = 400 МБ на канал, а не 1000/2 = 500.
    assert v["analytics_rss_mb_per_channel"] == 400.0
    assert v["machine_ram_pct"] == 55.0
    assert v["meets_ram_budget"] is True


def test_ram_budget_is_actually_enforced_in_the_verdict():
    m = _mod()
    base = {"machine_cpu_pct": 20.0, "machine_ram_pct": 30.0,
            "machine_ram_used_mb": 3000.0, "analytics_rss_mb": 200.0,
            "archive_mb_per_sec": 1.0, "record_cores_per_camera": 0.01,
            "record_rss_mb_per_camera": 9.0, "segments_written": 4}
    combined = dict(base, machine_ram_pct=88.0, machine_cpu_pct=50.0,
                    analytics={"fps_per_channel": 8.0, "meets_target": True})
    v = m._verdict(base, combined, cameras=8, analytics=2)
    assert v["meets_ram_budget"] is False


def test_record_ram_bracket_is_reported_even_when_outside():
    """MediaMTX — один процесс на все камеры, а вилка §16 писалась под
    раскладку «процесс на камеру». Расхождение ожидаемо, и замер обязан
    его показывать, а не молча считать норматив выполненным."""
    m = _mod()
    base = {"machine_cpu_pct": 20.0, "machine_ram_pct": 30.0,
            "machine_ram_used_mb": 3000.0, "analytics_rss_mb": 200.0,
            "archive_mb_per_sec": 1.0, "record_cores_per_camera": 0.01,
            "record_rss_mb_per_camera": 9.0, "segments_written": 4}
    combined = dict(base, analytics={"fps_per_channel": 8.0, "meets_target": True})
    v = m._verdict(base, combined, cameras=8, analytics=2)
    assert v["record_rss_mb_per_camera"] == 9.0
    assert v["record_ram_within_bracket"] is False
    assert v["record_ram_bracket_mb"] == [50.0, 100.0]
