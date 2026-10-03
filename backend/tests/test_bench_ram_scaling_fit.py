"""Арифметика `perf/bench_ram_scaling.py`: наклон, его граница, ускорение роста.

Зачем тестом, а не прогоном замера. Сам замер поднимает MediaMTX, N
публикующих ffmpeg и каналы аналитики с настоящей моделью — минуты и
внешние зависимости. Приближение прямой и решение «можно ли этот наклон
умножать на число камер объекта» — чистые функции над списками чисел;
проверять их прогоном значило бы не проверять вовсе (тот же довод, что в
`test_bench_load_worst.py` и `test_bench_chain_median.py`).

Почему это важнее обычной арифметики. На числах отсюда стоит
`backend/app/services/autoconfig.py` — калькулятор, который говорит
администратору, сколько камер держит его сервер. Ошибка в наклоне в
опасную сторону (занижен) даёт завышенное обещание вместимости, а
обнаруживается оно через сутки по пропущенным сегментам.
"""
import sys
from pathlib import Path

import pytest

PERF = Path(__file__).resolve().parents[2] / "perf"


def _mod():
    if str(PERF) not in sys.path:
        sys.path.insert(0, str(PERF))
    if not (PERF / "bench_ram_scaling.py").is_file():  # pragma: no cover
        pytest.skip("perf/bench_ram_scaling.py отсутствует")
    try:
        import bench_ram_scaling
    except ImportError as exc:
        # Отсутствующая зависимость — законный пропуск. Любая другая
        # ошибка импорта означает сломанный замер, и молча пропустить её
        # значило бы получить зелёный прогон на неработающем файле.
        if "psutil" in str(exc) or "cv2" in str(exc):
            pytest.skip(f"нет зависимости замера: {exc}")
        raise
    return bench_ram_scaling


# --- приближение прямой ----------------------------------------------------

def test_slope_and_intercept_are_recovered_from_a_clean_line():
    """На точном ряде наклон и свободный член восстанавливаются точно.

    Это не тавтология «формула считает формулу»: свободный член здесь —
    постоянная часть расхода (модель, рантайм), а наклон — стоимость
    камеры, и их РАЗДЕЛЕНИЕ и есть предмет замера. Цикл 46 их не
    разделял и получил «229.7 МБ на канал», где половина числа была
    общей моделью.
    """
    m = _mod()
    fit = m.fit_line([1, 2, 3, 4], [640, 660, 680, 700])
    assert fit["slope_mb_per_unit"] == pytest.approx(20.0)
    assert fit["base_mb"] == pytest.approx(620.0)


def test_two_points_in_the_same_n_are_refused():
    """Все точки в одном N — наклона нет, и притвориться, что есть, нельзя."""
    m = _mod()
    assert "error" in m.fit_line([2, 2, 2], [100, 110, 120])


def test_upper_bound_is_above_the_point_estimate_and_defined_only_with_repeats():
    """Верхняя граница наклона — то единственное, что идёт в калькулятор.

    Без повторов внутри уровня разброс оценить не из чего, и граница не
    определена. Молча подставить в этом случае саму оценку значило бы
    выдать «точно знаем» за «оценили» — и именно на этом числе стоит
    предложение вместимости.
    """
    m = _mod()
    noisy = m.fit_line([1, 1, 2, 2, 3, 3], [600, 610, 604, 598, 607, 601])
    assert noisy["slope_upper_mb_per_unit"] > noisy["slope_mb_per_unit"]
    assert noisy["slope_stderr_mb"] > 0

    only_two = m.fit_line([1, 2], [600, 610])
    assert only_two["slope_upper_mb_per_unit"] is None


def test_flat_growth_gives_a_small_positive_upper_bound_not_a_negative_slope():
    """Плоский расход: точечный наклон может выйти отрицательным.

    У слоя аналитики модель общая, и на канал приходятся одни буферы —
    наклон лежит на уровне шума. Отрицательную стоимость канала в
    калькулятор нести нельзя, но и округлять её до нуля нельзя тоже:
    ноль обещает бесконечную вместимость. Ответ — верхняя граница,
    которая на плоском ряде мала, но строго положительна.
    """
    m = _mod()
    fit = m.fit_line([1, 1, 2, 2, 3, 3, 4, 4],
                     [610, 600, 604, 612, 601, 608, 603, 606])
    assert fit["slope_mb_per_unit"] <= 1.0
    assert fit["slope_upper_mb_per_unit"] > 0


# --- ускорение роста -------------------------------------------------------

def test_accelerating_growth_is_caught():
    """Расход, растущий быстрее прямой, экстраполировать запрещено.

    Ряд 100/120/160/240 — удвоение приращения на каждом шаге. Первая
    редакция детектора (остаток дальней точки против разброса остатков)
    его НЕ ловила: кривизна раздувает тот самый разброс, с которым её
    сравнивают. Тест написан по этому промаху и падает на прежней
    редакции.
    """
    m = _mod()
    fit = m.fit_line([1, 1, 2, 2, 3, 3, 4, 4],
                     [100, 101, 120, 121, 160, 159, 240, 241])
    assert fit["superlinear"] is True


def test_straight_growth_is_not_flagged():
    """Ровная прямая ускорением не считается — иначе запрет бесполезен."""
    m = _mod()
    fit = m.fit_line([1, 1, 2, 2, 3, 3, 4, 4],
                     [100, 102, 150, 151, 200, 199, 250, 252])
    assert fit["superlinear"] is False


def test_flat_growth_is_not_flagged_as_accelerating():
    """Плоский ряд — не ускорение. Запретить его значило бы запретить
    ровно тот случай, ради которого замер и делался."""
    m = _mod()
    fit = m.fit_line([1, 1, 2, 2, 3, 3, 4, 4],
                     [610, 600, 604, 612, 601, 608, 603, 606])
    assert fit["superlinear"] is False


def test_without_repeats_the_shape_is_refused_rather_than_guessed():
    """Без повторов форму не различить, и ответ «не знаю» = «нельзя».

    Ошибка здесь идёт в опасную сторону: разрешив экстраполяцию по
    неизвестной форме, калькулятор пообещает камеры, которых сервер не
    вывезет.
    """
    m = _mod()
    fit = m.fit_line([1, 2, 3, 4], [100, 150, 200, 250])
    assert fit["superlinear"] is True


# --- вердикт ---------------------------------------------------------------

def _res(samples_y, bracket, xs=(1, 1, 2, 2, 3, 3, 4, 4)):
    m = _mod()
    return {"fit": m.fit_line(list(xs), list(samples_y)),
            "bracket_mb": list(bracket), "samples": []}


def test_verdict_refuses_to_hand_over_a_slope_it_cannot_vouch_for():
    """Ускоряющийся рост → `usable: False`, и причина названа словами.

    Молчаливое `usable: False` без причины кончилось бы тем, что
    следующий цикл прогонит замер, увидит пустое поле и решит, что замер
    сломан.
    """
    m = _mod()
    accelerating = _res([100, 101, 120, 121, 160, 159, 240, 241], (500.0, 2048.0))
    v = m.verdict({}, accelerating)["analytics"]
    assert v["usable"] is False
    assert v["why"]


def test_verdict_reports_how_far_the_spec_bracket_is_from_the_measurement():
    """Отношение «верх вилки §16 / верхняя граница замера» — это и есть
    множитель, на который калькулятор занижал вместимость сервера.
    Число должно быть в вердикте, а не оставаться в голове читающего."""
    m = _mod()
    flat = _res([610, 600, 604, 612, 601, 608, 603, 606], (500.0, 2048.0))
    v = m.verdict({}, flat)["analytics"]
    assert v["usable"] is True
    assert v["bracket_over_marginal"] > 1


def test_verdict_says_why_when_a_layer_was_not_measured_at_all():
    """Пропущенный замер отличим от замеренного нуля.

    `--skip-record` и «MediaMTX не нашёлся» дают одинаково пустой раздел;
    без явной причины отчёт следующего цикла записал бы это как «расход
    записи равен нулю».
    """
    m = _mod()
    v = m.verdict({"skipped": "нет MEDIAMTX_BIN"}, {})
    assert v["record"]["usable"] is False
    assert "MEDIAMTX_BIN" in v["record"]["why"]
    assert v["analytics"]["usable"] is False
    assert v["analytics"]["why"]


def test_brackets_match_the_ones_in_bench_load():
    """Вилки §16 продублированы в двух замерах — копии не должны разъехаться.

    `bench_load.py` сверяет с ними одну точку, `bench_ram_scaling.py` —
    наклон. Разъезд означал бы, что два замера сравнивают факт с разными
    «требованиями ТЗ».
    """
    m = _mod()
    sys.path.insert(0, str(PERF))
    import bench_load

    assert m.REC_RAM_BRACKET_MB == bench_load.REC_RAM_MB_PER_CAMERA
    assert m.ANALYTICS_RAM_BRACKET_MB == bench_load.ANALYTICS_RAM_MB_PER_CHANNEL
