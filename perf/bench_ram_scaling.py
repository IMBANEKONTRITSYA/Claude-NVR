#!/usr/bin/env python3
"""Как память обоих слоёв растёт с числом камер: замер НАКЛОНА, а не точки.

**Чего не хватало.** Цикл 46 впервые измерил §19 «RAM ≤ 80 %» и попутно
получил удельные числа: 4.98 МБ на камеру записи и 229.7 МБ на канал
аналитики. Оба — из ОДНОЙ точки (8 камер, 2 канала), и оба делением
общего RSS на число камер. Такое деление молча предполагает, что расход
пропорционален числу камер, то есть что постоянной части нет вовсе.

У обоих слоёв постоянная часть есть, и большая:

* запись — ОДИН процесс MediaMTX на все камеры (рантайм Go, кучи, пулы);
* аналитика — нити с ОБЩЕЙ моделью (`worker.manager()`), и вес модели
  делился на 2 канала, попав в «стоимость канала» половинками.

Отчёт цикла 46 это признал прямо: «229.7 МБ/канал — верхняя граница при
N = 2, включающая половину общей модели; правильная форма экстраполяции —
`RAM = база + модель + N × буферы`, а не `N × 229.7`. Мерить надо на
большем N». Этот замер и есть то мерение.

**Что меряется.** RSS под нагрузкой при нескольких значениях N, затем
наименьшие квадраты: `RSS(N) = base + slope × N`. `slope` — предельная
стоимость ОДНОЙ дополнительной камеры, единственное число, которое можно
умножать на число камер объекта. `base` — то, что платится один раз.

**Почему в калькулятор идёт не наклон, а его верхняя граница.** У слоя
аналитики модель общая, и на канал приходятся одни буферы: наклон лежит
на уровне шума аллокатора и точечной оценкой выходит то чуть выше нуля,
то чуть ниже. Отрицательную стоимость канала в калькулятор не понесёшь,
а округлить её до нуля значило бы пообещать бесконечную вместимость.
Поэтому считается стандартная ошибка наклона, и наружу отдаётся
`slope + 2·SE` — величина, которой расход на канал заведомо не
превышает. Отсюда же требование повторов (`--repeat`): без них
доверительной границы просто нет.

**Что запрещает экстраполяцию.** Опасна ровно одна форма нелинейности —
расход, растущий БЫСТРЕЕ прямой (своя арена на канал, фрагментация):
только она занижает стоимость камеры и потому завышает обещанную
вместимость. Ловится сравнением секущих — наклона между первыми двумя
уровнями N против наклона между последними двумя; поймав, вердикт
говорит «экстраполировать нельзя» вместо того, чтобы отдать наклон как
ни в чём не бывало. Низкий R² сам по себе запретом не является: плоская
прямая и есть ответ, а не отсутствие ответа.

**Зачем это нужно за пределами отчёта.** На этих числах стоит
`backend/app/services/autoconfig.py` — калькулятор, который предлагает
администратору, сколько камер держит его сервер. До цикла 47 он брал
верх вилок §16 (100 МБ и 2048 МБ на камеру) как удельную константу и
делил на неё всю память. Для аналитики это занижало вместимость почти на
порядок.

Запуск:

    MEDIAMTX_BIN=/путь/mediamtx python perf/bench_ram_scaling.py
    MEDIAMTX_BIN=... python perf/bench_ram_scaling.py --record-points 2,4,8 --json
    python perf/bench_ram_scaling.py --skip-record --analytics-points 1,2,3,4 --repeat 3
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from urllib import request

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "worker"))

import bench_remux as remux                                   # noqa: E402

# Вилки §16, с которыми сравнивается измеренное. Дублируются из
# perf/bench_load.py сознательно — там они про одну точку, здесь про
# наклон; расхождение стережёт тест.
REC_RAM_BRACKET_MB = (50.0, 100.0)
ANALYTICS_RAM_BRACKET_MB = (500.0, 2048.0)

# Частота выборок памяти. Как в bench_load.py: потолок — про худший
# момент окна, а не про тот, в который замер случайно посмотрел.
MEM_SAMPLE_SEC = 0.5

# Множитель стандартной ошибки для верхней границы наклона. Два сигмы —
# это ~95 % при нормальном шуме; брать одну значило бы отдавать в
# калькулятор величину, которую расход превышает в каждом шестом случае.
SLOPE_UPPER_SIGMAS = 2.0


def fit_line(xs: list[float], ys: list[float]) -> dict:
    """Наименьшие квадраты плюс то, по чему видно, что прямая не годится.

    Возвращает наклон, его верхнюю доверительную границу, свободный
    член, R², наибольший остаток в МБ и признак ускоряющегося роста.
    R² сам по себе обманчив (высок всегда, когда точки хоть как-то
    растут, и низок на плоской прямой, где всё в порядке), поэтому
    решение об экстраполяции принимается не по нему — см. `_superlinear`.
    """
    n = len(xs)
    if n < 2:
        return {"error": "нужно минимум две точки"}
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return {"error": "все точки в одном N"}
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    resid = [y - (intercept + slope * x) for x, y in zip(xs, ys)]
    ss_res = sum(r * r for r in resid)
    ss_tot = sum((y - my) ** 2 for y in ys)
    # ss_tot == 0 означает «расход не изменился ни на грамм»: прямая
    # горизонтальна и приближает идеально, но R² в этом случае не
    # определён (0/0), а не равен единице.
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    # Стандартная ошибка наклона. Она здесь не для красоты отчёта:
    # когда наклон близок к шуму (а у слоя аналитики он именно такой —
    # модель общая, на канал приходятся одни буферы), точечная оценка
    # может выйти и отрицательной, и её нельзя нести в калькулятор ни
    # как есть, ни округлив до нуля. Нести можно ВЕРХНЮЮ границу:
    # `slope + 2·SE` — то, чего расход на канал заведомо не превышает.
    se = None
    if n > 2 and sxx > 0:
        se = (ss_res / (n - 2) / sxx) ** 0.5
    return {
        "slope_mb_per_unit": round(slope, 2),
        # Верхняя доверительная граница наклона — единственное число
        # отсюда, которое разрешено умножать на число камер объекта.
        "slope_upper_mb_per_unit": (round(slope + SLOPE_UPPER_SIGMAS * se, 2)
                                    if se is not None else None),
        "slope_stderr_mb": round(se, 2) if se is not None else None,
        "base_mb": round(intercept, 1),
        "r2": round(r2, 4),
        "max_residual_mb": round(max(abs(r) for r in resid), 1),
        "points": n,
        "distinct_x": len(set(xs)),
        # Опасна ровно одна форма нелинейности — расход, растущий
        # БЫСТРЕЕ прямой: только она даёт заниженную стоимость камеры и,
        # значит, завышенное обещание вместимости. Как ловится — см.
        # `_superlinear`; низкий R² сам по себе запретом не является.
        "superlinear": _superlinear(xs, ys),
    }


def _superlinear(xs: list[float], ys: list[float]) -> bool:
    """Ускоряется ли рост: круче ли прямая на дальнем конце, чем на ближнем.

    **Не через остатки от общей прямой.** Первая редакция сравнивала
    остаток в дальней точке с их же разбросом и на явной параболе
    (100, 120, 160, 240) ускорения НЕ заметила: кривизна раздувает сам
    разброс, с которым её и сравнивают, поэтому порог уезжает вместе с
    сигналом. Поймано проверкой детектора на заведомо нелинейном ряде,
    а не на глаз.

    Здесь сравниваются секущие: наклон между двумя первыми уровнями N
    против наклона между двумя последними. Если расход растёт по прямой,
    секущие равны в пределах шума; если ускоряется — дальняя круче, и
    именно она задаёт цену камер, которых на замере не было.

    Шум берётся по повторам внутри уровня. Без повторов его неоткуда
    взять, и ответ тогда «не знаю» — а «не знаю» здесь означает
    «нельзя», потому что ошибка идёт в опасную сторону.
    """
    levels: dict[float, list[float]] = {}
    for x, y in zip(xs, ys):
        levels.setdefault(x, []).append(y)
    keys = sorted(levels)
    if len(keys) < 3:
        return True
    reps = [len(levels[k]) for k in keys]
    if min(reps) < 2:
        return True

    def mean(k):
        return sum(levels[k]) / len(levels[k])

    def sem(k):
        vals = levels[k]
        m = sum(vals) / len(vals)
        var = sum((v - m) ** 2 for v in vals) / (len(vals) - 1)
        return (var / len(vals)) ** 0.5

    near = (mean(keys[1]) - mean(keys[0])) / (keys[1] - keys[0])
    far = (mean(keys[-1]) - mean(keys[-2])) / (keys[-1] - keys[-2])
    # Шум секущей — от шума обоих её концов.
    near_sd = (sem(keys[0]) ** 2 + sem(keys[1]) ** 2) ** 0.5 / (keys[1] - keys[0])
    far_sd = (sem(keys[-2]) ** 2 + sem(keys[-1]) ** 2) ** 0.5 / (keys[-1] - keys[-2])
    noise = (near_sd ** 2 + far_sd ** 2) ** 0.5
    if noise <= 0:
        return far > near
    return (far - near) > SLOPE_UPPER_SIGMAS * noise


class _PeakRSS:
    """Пик RSS процесса (и, по требованию, машины) за окно.

    Пиком, а не чтением в конце: интересует худший момент. Нить дешёвая —
    одно-два чтения /proc в полсекунды.
    """

    def __init__(self, proc=None):
        import psutil
        self.psutil = psutil
        self.proc = proc or psutil.Process()

    def _take(self):
        try:
            self.peak_rss = max(self.peak_rss, self.proc.memory_info().rss)
        except Exception:
            pass
        vm = self.psutil.virtual_memory()
        self.peak_machine_pct = max(self.peak_machine_pct, vm.percent)

    def _loop(self):
        while not self._stop.wait(MEM_SAMPLE_SEC):
            self._take()

    def __enter__(self):
        self.peak_rss = 0
        self.peak_machine_pct = 0.0
        self._take()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join(timeout=MEM_SAMPLE_SEC * 4)
        self._take()

    @property
    def peak_mb(self) -> float:
        return round(self.peak_rss / 1048576, 1)


# --- слой записи -----------------------------------------------------------

def measure_record(points: list[int], warmup: int, measure: int,
                   repeat: int = 1) -> dict:
    """RSS MediaMTX при разном числе пишущихся камер.

    Один процесс поднимается ОДИН раз и камеры добавляются в него по
    ходу — так же, как на объекте, где камеры заводят в работающую
    систему, а не перезапускают сервер записи под каждую. Перезапуск на
    каждой точке дал бы другую величину: свежий рантайм Go против
    прогретого, и наклон вышел бы завышенным за счёт разогрева куч.

    `repeat` снимает КАЖДЫЙ уровень несколько раз (окно за окном, без
    перезапуска). Без повторов разброс внутри уровня неоткуда взять, а
    без него `_superlinear` обязан отвечать «не знаю» — то есть
    «экстраполировать нельзя», и замер оказывается бесполезен для
    калькулятора, сколь угодно гладко ни выглядели бы точки.
    """
    import psutil

    if not remux.MEDIAMTX_BIN or not os.path.exists(remux.MEDIAMTX_BIN):
        return {"skipped": "нет MEDIAMTX_BIN — нужен настоящий MediaMTX"}
    if not shutil.which("ffmpeg"):
        return {"skipped": "нет ffmpeg"}

    remux.make_sample()
    segdir = remux.SEGDIR
    shutil.rmtree(segdir, ignore_errors=True)
    os.makedirs(segdir, exist_ok=True)
    conf = os.path.join(remux.WORK, "mediamtx_ramscale.yml")
    with open(conf, "w") as fh:
        fh.write(remux.mediamtx_conf())

    mtx = subprocess.Popen([remux.MEDIAMTX_BIN, conf],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    proc = psutil.Process(mtx.pid)
    pubs: list[subprocess.Popen] = []
    samples: list[dict] = []
    started = 0
    try:
        for target in sorted(points):
            for i in range(started, target):
                # Продакшн-раскладка: источник публикуется, записывающий
                # путь его ТЯНЕТ — как воркер заводит настоящую камеру.
                remux.api_post(f"/v3/config/paths/add/src{i}",
                               {"source": "publisher", "record": False})
                remux.api_post(f"/v3/config/paths/add/cam{i}",
                               remux.pull_path_conf(f"{remux.RTSP}/src{i}"))
                pubs.append(subprocess.Popen([
                    "ffmpeg", "-re", "-stream_loop", "-1", "-i", remux.SAMPLE,
                    "-c", "copy", "-f", "rtsp", f"{remux.RTSP}/src{i}",
                ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
                time.sleep(0.15)
            started = target
            time.sleep(warmup)
            with request.urlopen(f"{remux.API}/v3/paths/list", timeout=10) as r:
                paths = json.load(r)
            ready = sum(1 for p in paths["items"]
                        if p.get("ready") and str(p.get("name", "")).startswith("cam"))
            if ready != target:
                return {"error": f"готовы {ready} из {target} записывающих путей"}
            for _ in range(max(1, repeat)):
                with _PeakRSS(proc) as peak:
                    time.sleep(measure)
                samples.append({"cameras": target, "rss_mb": peak.peak_mb,
                                "machine_ram_pct": round(peak.peak_machine_pct, 1)})
    finally:
        for p in pubs:
            p.terminate()
        for p in pubs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        mtx.terminate()
        try:
            mtx.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mtx.kill()
        shutil.rmtree(segdir, ignore_errors=True)

    fit = fit_line([s["cameras"] for s in samples], [s["rss_mb"] for s in samples])
    return {"layer": "record", "samples": samples, "fit": fit,
            "bracket_mb": list(REC_RAM_BRACKET_MB)}


# --- слой аналитики --------------------------------------------------------

CHILD_ENV_MARKER = "BENCH_RAM_SCALING_CHILD"


def _analytics_child(clip: str, channels: int, seconds: float) -> dict:
    """Одна точка замера аналитики — целиком внутри СВОЕГО процесса.

    Здесь и загрузка модели, и снятие базы, и прогон каналов. Результат
    уходит родителю строкой JSON в stdout.
    """
    import psutil
    import bench_scaling

    proc = psutil.Process()
    # База — интерпретатор с импортами, ДО модели. Модель грузит сам
    # `run_threads`, ровно один экземпляр на любое число каналов, значит
    # её вес — постоянная часть и место ему в свободном члене прямой, а
    # не в наклоне. Отдельного прогрева здесь нет намеренно: `run_threads`
    # греет каждый канал до барьера сам, а лишний экземпляр модели,
    # созданный «на прогрев», осел бы в аренах ORT и завысил свободный
    # член на свой полный вес.
    base_mb = round(proc.memory_info().rss / 1048576, 1)

    with _PeakRSS(proc) as peak:
        with _quiet():
            res = bench_scaling.run_threads(clip, channels, seconds)
    return {
        "channels": channels,
        "rss_mb": peak.peak_mb,
        "baseline_before_model_mb": base_mb,
        "growth_mb": round(peak.peak_mb - base_mb, 1),
        "fps_per_channel": res.get("fps_per_channel"),
        "machine_ram_pct": round(peak.peak_machine_pct, 1),
        "error": res.get("error"),
    }


class _quiet:
    """Подавить болтовню insightface: она идёт в stdout, а там JSON."""

    def __enter__(self):
        self._fd = os.dup(1)
        self._null = os.open(os.devnull, os.O_WRONLY)
        os.dup2(self._null, 1)

    def __exit__(self, *exc):
        os.dup2(self._fd, 1)
        os.close(self._null)
        os.close(self._fd)


def measure_analytics(points: list[int], seconds: float,
                      repeat: int = 1) -> dict:
    """Прирост RSS при разном числе каналов аналитики — точка на процесс.

    Каналы внутри точки поднимаются нитями с общей моделью — той же
    конструкцией, что в `worker.manager()`; иначе мерилась бы раскладка,
    которой в проде нет.

    **Каждая точка — отдельный процесс, и это не перестраховка.**
    `bench_scaling.run_threads()` создаёт СВОЙ экземпляр модели на каждый
    вызов, а арены ORT, однажды выросшие, операционной системе не
    возвращаются. Три вызова подряд в одном процессе дают RSS, который
    растёт от самого факта повторных вызовов, — и наклон такой прямой
    измерял бы накопление арен пополам со стоимостью канала. Разделение
    по процессам убирает связь между точками целиком: каждая начинает с
    чистого аллокатора и сама платит за свою модель.

    Первая редакция этого замера ошибку содержала (один процесс, модель
    грузилась заново на каждой точке) и была переписана до публикации
    чисел — ровно тот класс ошибки, из-за которого цикл 46 получил
    «229.7 МБ на канал», где половина числа была общей моделью.
    """
    try:
        import bench_scaling                              # noqa: F401
    except ImportError as exc:                     # pragma: no cover
        return {"skipped": f"нет зависимостей аналитики: {exc}"}
    import bench

    clip = os.path.join(remux.WORK, "faces_ramscale.mp4")
    os.makedirs(remux.WORK, exist_ok=True)
    if not os.path.exists(clip):
        made = bench.make_face_clip(clip)
        if made.get("error"):
            return {"error": made["error"]}

    samples: list[dict] = []
    for n in sorted(points) * max(1, repeat):
        env = dict(os.environ, **{CHILD_ENV_MARKER: "1"})
        child = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--child-point",
             str(n), "--child-clip", clip, "--child-seconds", str(seconds)],
            capture_output=True, text=True, env=env, timeout=seconds * 20 + 600)
        if child.returncode != 0:
            return {"error": f"точка N={n} упала: {child.stderr[-400:]}"}
        try:
            got = json.loads(child.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": f"точка N={n} не вернула JSON: {child.stdout[-400:]}"}
        if got.get("error"):
            return {"error": got["error"]}
        samples.append(got)

    fit = fit_line([s["channels"] for s in samples], [s["rss_mb"] for s in samples])
    return {"layer": "analytics", "samples": samples, "fit": fit,
            "point_per_process": True,
            "bracket_mb": list(ANALYTICS_RAM_BRACKET_MB)}


# --- сведение --------------------------------------------------------------

def verdict(record: dict, analytics: dict) -> dict:
    """Что из замеренного можно нести в калькулятор, а что нельзя."""
    out: dict = {}
    for key, res in (("record", record), ("analytics", analytics)):
        if not res or res.get("skipped") or res.get("error"):
            out[key] = {"usable": False,
                        "why": (res or {}).get("skipped") or (res or {}).get("error")
                        or "замер не выполнялся"}
            continue
        fit = res["fit"]
        if fit.get("error"):
            out[key] = {"usable": False, "why": fit["error"]}
            continue
        lo, hi = res["bracket_mb"]
        upper = fit.get("slope_upper_mb_per_unit")
        usable = not fit["superlinear"] and upper is not None
        out[key] = {
            "usable": usable,
            "why": None if usable else (
                "расход растёт быстрее прямой — наклон экстраполировать нельзя"
                if fit["superlinear"] else
                f"точек мало ({fit['points']}), доверительная граница наклона "
                "не определена: нужны повторы"),
            # Точечная оценка — для отчёта; в калькулятор идёт только
            # верхняя граница.
            "marginal_mb_per_unit": fit["slope_mb_per_unit"],
            "marginal_upper_mb_per_unit": upper,
            "constant_mb": fit["base_mb"],
            "spec_bracket_mb": [lo, hi],
            # Во сколько раз вилка §16 выше ВЕРХНЕЙ границы предельной
            # стоимости. Это отношение и есть коэффициент, на который
            # калькулятор занижает вместимость сервера.
            "bracket_over_marginal": (round(hi / upper, 1)
                                      if upper and upper > 0 else None),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record-points", default="2,4,8",
                    help="числа камер записи через запятую")
    ap.add_argument("--analytics-points", default="1,2,3,4",
                    help="числа каналов аналитики через запятую")
    ap.add_argument("--warmup", type=int, default=8)
    ap.add_argument("--measure", type=int, default=12)
    ap.add_argument("--analytics-seconds", type=float, default=12.0)
    ap.add_argument("--repeat", type=int, default=1,
                    help="повторов каждого уровня N: без повторов "
                         "доверительная граница наклона не определена")
    ap.add_argument("--skip-record", action="store_true")
    ap.add_argument("--skip-analytics", action="store_true")
    ap.add_argument("--json", action="store_true")
    # Служебный режим: одна точка аналитики в своём процессе (см.
    # measure_analytics). Родитель зовёт сам себя с этими аргументами.
    ap.add_argument("--child-point", type=int, help=argparse.SUPPRESS)
    ap.add_argument("--child-clip", help=argparse.SUPPRESS)
    ap.add_argument("--child-seconds", type=float, help=argparse.SUPPRESS)
    a = ap.parse_args()

    if a.child_point is not None:
        print(json.dumps(_analytics_child(a.child_clip, a.child_point,
                                          a.child_seconds), ensure_ascii=False))
        return 0

    def _points(raw: str) -> list[int]:
        return sorted({int(x) for x in raw.split(",") if x.strip()})

    rec = {} if a.skip_record else measure_record(
        _points(a.record_points), a.warmup, a.measure, a.repeat)
    ana = {} if a.skip_analytics else measure_analytics(
        _points(a.analytics_points), a.analytics_seconds, a.repeat)
    out = {"record": rec, "analytics": ana, "verdict": verdict(rec, ana),
           "cpu_count": os.cpu_count()}

    if a.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    for key, title in (("record", "СЛОЙ ЗАПИСИ (MediaMTX, один процесс)"),
                       ("analytics", "СЛОЙ АНАЛИТИКИ (нити, общая модель)")):
        res = out[key]
        print(f"\n=== {title} ===")
        if not res or res.get("skipped") or res.get("error"):
            print(f"  пропущен: {(res or {}).get('skipped') or (res or {}).get('error')}")
            continue
        unit = "камер" if key == "record" else "каналов"
        for s in res["samples"]:
            n = s.get("cameras", s.get("channels"))
            print(f"  {n:>3} {unit}: RSS {s['rss_mb']:>8.1f} МБ   "
                  f"машина {s['machine_ram_pct']:>5.1f} %")
        fit = res["fit"]
        v = out["verdict"][key]
        print(f"  прямая: RSS = {fit['base_mb']} + {fit['slope_mb_per_unit']} × N   "
              f"R² {fit['r2']}   промах ≤ {fit['max_residual_mb']} МБ")
        if v["usable"]:
            print(f"  вилка §16 {v['spec_bracket_mb'][0]:.0f}-{v['spec_bracket_mb'][1]:.0f} МБ "
                  f"выше предельной стоимости в {v['bracket_over_marginal']} раза")
        else:
            print(f"  НЕПРИГОДНО для экстраполяции: {v['why']}")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
