"""Сведение повторов замера цепочки в медиану (`perf/bench.py`) — цикл 39.

Зачем медиана. Цикл 38 занёс в carryover наблюдение: `chain_fps` на общих
раннерах GitHub гуляет ±20 % при **нетронутом** коде аналитики — 8.17 →
6.41 → 6.75 за три цикла подряд, и ни один PR тех циклов не касался ни
воркера, ни `perf/bench.py`, ни пути аналитики. Правило «деградация больше
15 % против прошлого цикла — carryover P2» на одиночном прогоне поэтому
срабатывало на шуме и с тем же успехом промолчало бы о настоящей просадке
той же величины.

Сама медиана тестируется здесь, а не в замере: прогон цепочки тянет модель
buffalo_s (~125 МБ) и идёт минуты, а сведение результатов — чистая функция
над словарями, и проверять её этим прогоном значило бы не проверять вовсе.
"""
import sys
from pathlib import Path

import pytest

PERF = Path(__file__).resolve().parents[2] / "perf"


def _bench_module():
    if str(PERF) not in sys.path:
        sys.path.insert(0, str(PERF))
    try:
        import bench  # noqa: PLC0415
    except Exception as exc:  # cv2/numpy нет в лёгком окружении
        pytest.skip(f"perf/bench.py не импортируется: {exc}")
    return bench


def _run(fps: float, cores: float = 0.8, ms_detect: float = 140.0) -> dict:
    """Результат одного прогона в той форме, в какой его отдаёт
    `bench_analytics_chain()` — поля, от которых зависит сведение."""
    return {
        "chain_fps": fps,
        "cores_per_camera_at_target": cores,
        "cores_per_camera_spec": 1.5,
        "ms_cpu_per_frame": 155.0,
        "ms_detect_embed": ms_detect,
        "ms_detect_embed_p95": ms_detect + 2,
        "ms_decode": 3.0,
        "ms_prefilter": 2.0,
        "frames": 300,
        "detector_runs": 200,
        "detector_duty": 0.667,
        "faces_detected": 200,
        "target_fps": 5.0,
        "meets_target": fps >= 5.0,
        "meets_cores_budget": cores <= 1.5,
        "single_thread": True,
        "model": "buffalo_s",
        "det_size": 640,
    }


def test_median_not_mean_ignores_a_single_outlier():
    """Главное свойство: один выброс (сосед по гипервизору забрал ядро) не
    двигает результат. Среднее из 6.5/6.7/2.0 — 5.07, медиана — 6.5."""
    bench = _bench_module()
    out = bench.median_of_runs([_run(6.5), _run(6.7), _run(2.0)])
    assert out["chain_fps_median"] == 6.5


def test_verdicts_are_taken_from_the_median_not_from_one_run():
    """§19 «≥ 5 FPS/канал» судится по тому же числу, которое цикл заносит в
    отчёт. Иначе один провальный прогон красил бы вердикт при здоровой
    медиане — и наоборот."""
    bench = _bench_module()
    out = bench.median_of_runs([_run(6.0), _run(6.4), _run(2.0)])
    assert out["chain_fps_median"] == 6.0
    assert out["meets_target"] is True

    bad = bench.median_of_runs([_run(4.0), _run(4.2), _run(9.9)])
    assert bad["chain_fps_median"] == 4.2
    assert bad["meets_target"] is False


def test_cores_budget_verdict_also_uses_the_median():
    """Второй норматив того же замера (§16, вилка 0.5–1.5 ядра/камеру)
    обязан считаться так же, иначе два вердикта одного прогона разъедутся."""
    bench = _bench_module()
    out = bench.median_of_runs([_run(6.0, cores=0.8), _run(6.1, cores=0.9),
                                _run(6.2, cores=4.0)])
    assert out["cores_per_camera_at_target_median"] == 0.9
    assert out["meets_cores_budget"] is True


def test_spread_is_reported_so_noise_is_visible():
    """Разброс печатается намеренно: если он сам больше порога деградации
    (15 %), сравнение с прошлым циклом по этому числу недостоверно, и знать
    об этом надо сразу, а не через цикл."""
    bench = _bench_module()
    out = bench.median_of_runs([_run(6.0), _run(6.6), _run(7.2)])
    assert out["spread_pct"] == pytest.approx(20.0, abs=0.1)
    assert out["chain_fps_runs"] == [6.0, 6.6, 7.2]


def test_representative_run_keeps_its_own_fields_consistent():
    """Не-медианные поля берутся из ОДНОГО прогона — того, что ближе к
    медиане. Склеивать `detector_duty` одного прогона с `faces_detected`
    другого нельзя: числа перестали бы описывать один замер."""
    bench = _bench_module()
    runs = [_run(6.0, ms_detect=150.0), _run(6.5, ms_detect=140.0),
            _run(9.0, ms_detect=90.0)]
    runs[1]["faces_detected"] = 222
    runs[1]["detector_runs"] = 210

    out = bench.median_of_runs(runs)

    assert out["chain_fps"] == 6.5, "представителем взят не прогон-медиана"
    assert out["faces_detected"] == 222 and out["detector_runs"] == 210


def test_single_run_stays_backward_compatible():
    """`--repeat 1` (умолчание) обязан давать прежнюю форму результата:
    отчёты прошлых циклов и джоба CI читают `chain_fps`."""
    bench = _bench_module()
    out = bench.median_of_runs([_run(6.75)])
    assert out["chain_fps"] == 6.75
    assert out["repeats"] == 1
    assert "chain_fps_median" not in out


def test_failed_runs_do_not_poison_the_median():
    """Прогон, упавший на середине (ошибка декодера, убитый процесс), не
    должен ни ронять сведение, ни считаться нулём."""
    bench = _bench_module()
    out = bench.median_of_runs([{"error": "клип не содержит кадров"},
                                _run(6.0), _run(6.4)])
    assert out["repeats"] == 2
    assert out["chain_fps_median"] == pytest.approx(6.2)


def test_all_runs_failed_is_reported_not_swallowed():
    """Позитивный контроль к предыдущему: если не удался ни один прогон,
    сведение обязано вернуть ошибку, а не выдуманное число."""
    bench = _bench_module()
    out = bench.median_of_runs([{"error": "нет ffmpeg"}])
    assert "error" in out
