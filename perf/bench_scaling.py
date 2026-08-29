#!/usr/bin/env python3
"""Масштабирование слоя аналитики по каналам: нити против процессов (SPEC §17, §19).

**Зачем замер.** Слой аналитики устроен на нитях: `worker.manager()` поднимает
по `threading.Thread` на камеру, и все они дёргают **общий** `FACE_APP`.
Стандартное возражение — GIL: питоновские нити не исполняют байткод
параллельно, поэтому «камеры надо развести по процессам». Возражение
разумное, но проверяемое, и до цикла 40 его никто не проверял прямым
сравнением: цикл 36 померил только нити (см. `worker/ort_threads.py`), а
процессы — нет.

Замер отвечает на два вопроса, каждый из которых меняет архитектуру:

1. **Даёт ли переход на процессы прирост FPS?** Если да — слой надо
   переписывать на `multiprocessing`. Если нет — нити остаются, и это
   решение подкреплено числом, а не рассуждением про GIL.
2. **Сколько стоит переход по памяти?** Нити делят одну модель, процессы —
   нет. На 64-поточном сервере §20 это разница между одной копией весов и
   N копиями, и её надо знать до, а не после.

**Почему GIL здесь может ничего не решать.** Все три горячих шага цепочки
уходят в C и GIL на это время отпускают: `cap.read()` (FFmpeg внутри
OpenCV), `MOG2.apply()`, `session.run()` (ONNX Runtime). Под GIL остаётся
только склейка между ними. Так это или нет — вопрос к замеру.

**Что меряется.** Ровно та цепочка, что в `worker.camera_worker()` и в
`bench.py:bench_analytics_chain()` — decode 720p H.265 → resize → MOG2 →
`FACE_APP.get()` — но K каналов **одновременно**, и результат считается
суммарный. Модель в обоих режимах ограничена одним потоком ORT
(`ort_threads.limit_threads(1)`), как в production.

Загрузка модели и первый (прогревочный) кадр из замера исключены: каналы
синхронизируются барьером и стартуют вместе. Иначе режим процессов платил
бы за N загрузок модели тем временем, которое к пропускной способности
отношения не имеет.

Запуск:

    python perf/bench_scaling.py                 # 1,2,4 канала, оба режима
    python perf/bench_scaling.py --channels 1 2  # свой набор
    python perf/bench_scaling.py --json          # для CI

**Оговорка про песочницу.** Числа сняты на 4 ядрах с AVX2; у целевого
E5-2670 (§20) AVX2 нет и ядер 64. Абсолютные значения на сервер не
переносятся — переносится **отношение** режимов, потому что оба меряются
на одном железе одним прогоном.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing as mp
import os
import statistics
import sys
import tempfile
import threading as _threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "worker"))

# Целевой FPS канала (§19: «Детекция ≥ 5 FPS/канал»).
DETECTION_FPS_TARGET = 5.0


_QUIET_LOCK = _threading.Lock()
_quiet_state = {"depth": 0, "saved": -1}


@contextlib.contextmanager
def _quiet_stdout():
    """Увести вывод загрузчика моделей со stdout на stderr.

    insightface печатает состав модели через `print()`, а дочерние процессы
    режима `processes` наследуют тот же stdout. С `--json` это смешивало бы
    отчёт с логом и ломало разбор в CI. Подменяется дескриптор, а не
    `sys.stdout`: печатают в том числе C-расширения.

    Со счётчиком и замком, потому что дескриптор 1 — один на процесс, а
    прогрев в режиме `threads` идёт из K нитей сразу. Наивная версия
    (`dup`/`dup2` без синхронизации) при этом теряет исходный stdout
    навсегда: вторая нить сохраняет уже подменённый дескриптор и
    «восстанавливает» его последней. Замечено на первом же прогоне —
    таблица результатов ушла в никуда.
    """
    global _quiet_state
    with _QUIET_LOCK:
        if _quiet_state["depth"] == 0:
            _quiet_state["saved"] = os.dup(1)
            os.dup2(2, 1)
        _quiet_state["depth"] += 1
    try:
        yield
    finally:
        with _QUIET_LOCK:
            _quiet_state["depth"] -= 1
            if _quiet_state["depth"] == 0:
                os.dup2(_quiet_state["saved"], 1)
                os.close(_quiet_state["saved"])
                _quiet_state["saved"] = -1


def _prepare_app():
    """Загрузить модель ровно так, как это делает `worker.load_face_app()`.

    Важны обе детали: `allowed_modules` (без них FaceAnalysis тянет ещё
    четыре сети, и цепочка становится в шесть раз дороже — см. worker.py)
    и ограничение пула ORT одним потоком (см. `worker/ort_threads.py`).
    """
    import cv2
    from insightface.app import FaceAnalysis

    try:
        import ort_threads
        ort_threads.limit_threads(1)
    except ImportError:
        pass
    cv2.setNumThreads(1)
    app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"],
                       allowed_modules=["detection", "recognition"])
    app.prepare(ctx_id=0, det_size=(640, 640))
    return app


def _run_channel(app, clip: str, stop_after: float) -> dict:
    """Один канал аналитики: та же последовательность, что в camera_worker().

    `stop_after` — настенный дедлайн, общий у всех каналов. Ограничение по
    времени, а не по числу кадров, намеренно: при ограничении по кадрам
    быстрый канал заканчивал бы раньше и освобождал ядро остальным, то
    есть замер мерил бы уже не K одновременных каналов.
    """
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(clip)
    if not cap.isOpened():
        return {"error": "VideoCapture не открыл клип"}
    bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25,
                                            detectShadows=False)
    frames = detector_runs = faces = 0
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    while time.perf_counter() < stop_after:
        ok, frame = cap.read()
        if not ok:
            # Клип короче окна замера — крутим его по кругу, как крутится
            # бесконечный поток с камеры.
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            ok, frame = cap.read()
            if not ok:
                break
        frames += 1
        small = cv2.resize(frame, (640, 360))
        if int(np.count_nonzero(bg.apply(small))) > 1500:
            detector_runs += 1
            faces += len(app.get(frame))
    wall = time.perf_counter() - t0
    cpu = time.process_time() - cpu0
    cap.release()
    return {"frames": frames, "wall_sec": wall, "cpu_sec": cpu,
            "detector_runs": detector_runs, "faces": faces}


# --- режим «нити» (как сейчас в production) -------------------------------

def run_threads(clip: str, channels: int, seconds: float) -> dict:
    import threading

    with _quiet_stdout():
        app = _prepare_app()
    results: list[dict] = [{} for _ in range(channels)]
    ready = threading.Barrier(channels + 1)

    def worker(idx: int):
        # Прогрев до барьера: первый вызов инференса дороже последующих
        # (аллокации арен ORT), и без прогрева он целиком попал бы в замер
        # самого короткого прогона.
        import cv2
        cap = cv2.VideoCapture(clip)
        ok, frame = cap.read()
        cap.release()
        if ok:
            with _quiet_stdout():
                app.get(frame)
        ready.wait()
        # Дедлайн считает каждый канал сам, сразу после барьера. Общая
        # переменная, выставляемая главной нитью после `ready.wait()`, здесь
        # была гонкой: барьер отпускает всех одновременно, и канал успевал
        # прочитать ноль — то есть заканчивался, не начавшись.
        results[idx] = _run_channel(app, clip, time.perf_counter() + seconds)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(channels)]
    for t in threads:
        t.start()
    ready.wait()
    # CPU-время снимается ЗДЕСЬ, в главной нити, одним замером на весь
    # режим — и это не косметика. `time.process_time()` считает время
    # ПРОЦЕССА по всем его нитям, поэтому замер внутри каждого канала с
    # последующим суммированием давал бы K-кратное завышение: первая
    # редакция замера показала «14.78 занятых ядра» на четырёхъядерной
    # машине. У режима процессов такой ошибки нет (у каждого канала свой
    # процесс), из-за чего сравнение режимов получалось не в пользу нитей
    # ровно на множитель K.
    cpu0 = time.process_time()
    t0 = time.perf_counter()
    for t in threads:
        t.join()
    wall = time.perf_counter() - t0
    total_cpu = time.process_time() - cpu0
    return _summarize("threads", results, wall, channels, rss_mb=_rss_mb(),
                      total_cpu=total_cpu)


# --- режим «процессы» (гипотеза «GIL мешает») ------------------------------

def _proc_entry(idx: int, clip: str, seconds: float, cpus: list[int] | None,
                ready, out):
    """Тело дочернего процесса: своя модель, свой пул ORT, своя аффинность."""
    if cpus and hasattr(os, "sched_setaffinity"):
        try:
            os.sched_setaffinity(0, set(cpus))
        except OSError:
            pass
    try:
        import cv2
        with _quiet_stdout():
            app = _prepare_app()
            cap = cv2.VideoCapture(clip)
            ok, frame = cap.read()
            cap.release()
            if ok:
                app.get(frame)
    except Exception as exc:                     # pragma: no cover
        out.put((idx, {"error": f"{type(exc).__name__}: {exc}"}))
        ready.wait()
        return
    ready.wait()
    # Дедлайн отсчитывается от выхода из барьера, а не от общей переменной,
    # которую главный процесс выставляет после него: барьер отпускает всех
    # одновременно, и чтение опережало запись (та же гонка, что в нитях).
    out.put((idx, _run_channel(app, clip, time.perf_counter() + seconds)))


def run_processes(clip: str, channels: int, seconds: float,
                  pin: bool = True) -> dict:
    ctx = mp.get_context("spawn")
    ready = ctx.Barrier(channels + 1)
    out = ctx.Queue()
    cpu_sets = _cpu_sets(channels) if pin else [None] * channels
    procs = [ctx.Process(target=_proc_entry,
                         args=(i, clip, seconds, cpu_sets[i], ready, out),
                         daemon=True)
             for i in range(channels)]
    for p in procs:
        p.start()
    ready.wait()
    t0 = time.perf_counter()
    rss_peak = _rss_mb(children=procs)
    results: list[dict] = [{} for _ in range(channels)]
    got = 0
    while got < channels:
        idx, res = out.get()
        results[idx] = res
        got += 1
        # Пик памяти снимается пока процессы ещё живы: после join() читать
        # уже нечего, а именно N копий весов модели — цена этого режима.
        rss_peak = max(rss_peak, _rss_mb(children=procs))
    wall = time.perf_counter() - t0
    for p in procs:
        p.join(timeout=30)
    # Здесь суммирование по каналам корректно: у каждого свой процесс, и
    # `process_time()` внутри него считает только его собственное время.
    total_cpu = sum(r.get("cpu_sec", 0.0) for r in results if not r.get("error"))
    summary = _summarize("processes" + ("+pinned" if pin else ""), results,
                         wall, channels, rss_mb=rss_peak, total_cpu=total_cpu)
    summary["pinned"] = bool(pin)
    summary["cpu_sets"] = cpu_sets if pin else None
    return summary


def _cpu_sets(channels: int) -> list[list[int]]:
    """Раздать каналам непересекающиеся **блоки** ядер.

    Раскладка по NUMA-нодам здесь не воспроизводится — в песочнице нода
    одна. Смысл пиннинга в замере другой: снять с планировщика право
    таскать канал между ядрами, чтобы режимы отличались только моделью
    параллелизма.

    **Блоками, а не по одному ядру на канал** — и это исправление, а не
    вкусовщина. Первая редакция выдавала каналу ровно одно ядро, и на
    раннере CI (AMD EPYC 7763, 4 vCPU) режим процессов показал 3.82 FPS на
    ядро против 6.20 у нитей — то есть якобы вдвое хуже, тогда как в
    песочнице (Intel, 4 vCPU) те же режимы дали 4.60 против 4.96. Разница
    между машинами объясняется не моделью параллелизма, а SMT:
    `thread_siblings_list` в песочнице показывает по одному номеру на
    группу (siblings не видны), а на раннере два соседних vCPU вполне
    могут оказаться двумя нитями ОДНОГО физического ядра — и два процесса,
    прибитых к номерам 0 и 1, делят одно ядро, пока нити свободно
    расходятся по всем четырём.

    То есть замер сравнивал не «нити против процессов», а «две нити
    одного ядра против четырёх vCPU». Блоки убирают этот перекос: каждый
    канал получает равную долю машины, как в production канал получает
    ядра своей ноды целиком (`worker/cpu_affinity.py` — по той же причине
    привязывает к ноде, а не к ядру).
    """
    try:
        avail = sorted(os.sched_getaffinity(0))
    except AttributeError:                       # pragma: no cover
        avail = list(range(os.cpu_count() or 1))
    if not avail or channels <= 0:
        return [None] * max(channels, 0)         # type: ignore[list-item]
    if channels >= len(avail):
        # Ядер меньше каналов — делить нечего, раздаём по кругу.
        return [[avail[i % len(avail)]] for i in range(channels)]
    out: list[list[int]] = []
    total = len(avail)
    for i in range(channels):
        lo = i * total // channels
        hi = (i + 1) * total // channels
        out.append(avail[lo:hi])
    return out


def _rss_mb(children=None) -> float | None:
    """RSS процесса замера (и дочерних, если они есть), МБ."""
    try:
        import psutil
    except ImportError:
        return None
    total = psutil.Process().memory_info().rss
    for p in children or []:
        try:
            total += psutil.Process(p.pid).memory_info().rss
        except Exception:
            pass
    return round(total / 1048576, 1)


def _summarize(mode: str, results: list[dict], wall: float, channels: int,
               rss_mb: float | None, total_cpu: float) -> dict:
    """Свести каналы режима в одну строку.

    `total_cpu` приходит снаружи, а не суммируется здесь по каналам:
    корректный способ его получить у нитей и у процессов разный (см.
    комментарий в `run_threads`).
    """
    errors = [r.get("error") for r in results if r.get("error")]
    ok = [r for r in results if not r.get("error") and r.get("frames")]
    if not ok:
        return {"mode": mode, "channels": channels, "error": errors or "нет результатов"}
    total_frames = sum(r["frames"] for r in ok)
    per_channel = [r["frames"] / r["wall_sec"] for r in ok]
    return {
        "mode": mode,
        "channels": channels,
        "wall_sec": round(wall, 2),
        "total_fps": round(total_frames / wall, 2),
        "fps_per_channel": round(statistics.mean(per_channel), 2),
        "fps_per_channel_min": round(min(per_channel), 2),
        # Ядра, занятые слоем целиком: CPU-секунды всех каналов на настенное
        # время. На 4 ядрах значение около 4 означает, что упёрлись в машину.
        "cores_busy": round(total_cpu / wall, 2),
        # Главная величина сравнения режимов: сколько кадров даёт одно ядро.
        # Суммарный FPS сам по себе сравнивать нельзя — режимы могут занять
        # разное число ядер.
        "fps_per_core": round(total_frames / total_cpu, 2) if total_cpu else None,
        "cores_per_camera_at_target": round(total_cpu / total_frames * DETECTION_FPS_TARGET, 3)
        if total_frames else None,
        "rss_mb": rss_mb,
        "meets_target": min(per_channel) >= DETECTION_FPS_TARGET,
        "frames": total_frames,
        # Сторож против ошибки учёта CPU, на которой первая редакция замера
        # намерила 14.78 занятых ядра на четырёх ядрах. Занять больше ядер,
        # чем разрешено процессу, невозможно физически — если число вышло
        # такое, сравнивать режимы по нему нельзя, и это должно быть видно
        # в результате, а не только в глазах читающего.
        "impossible_cores": round(total_cpu / wall, 2) > len(_allowed_cpus()) + 0.5,
        "errors": errors or None,
    }


def _allowed_cpus() -> set[int]:
    try:
        return set(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        return set(range(os.cpu_count() or 1))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--channels", type=int, nargs="+", default=[1, 2, 4],
                    help="сколько каналов аналитики гонять одновременно")
    ap.add_argument("--seconds", type=float, default=20.0,
                    help="длительность окна замера на каждую точку")
    ap.add_argument("--modes", nargs="+", default=["threads", "processes"],
                    choices=["threads", "processes", "processes-unpinned"])
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    log = (lambda *a: None) if args.json else (lambda *a: print(*a, file=sys.stderr))

    import bench                                  # perf/bench.py рядом
    tmp = tempfile.mkdtemp(prefix="fw-scaling-")
    clip = os.path.join(tmp, "faces.mp4")
    log("готовлю клип с лицами (H.265 720p 15 fps, §1)...")
    made = bench.make_face_clip(clip)
    if made.get("error"):
        print(json.dumps({"error": made["error"]}), flush=True)
        return 1

    out: dict = {"clip": made, "cpu": _cpu_model(), "cpu_count": os.cpu_count(),
                 "numa_nodes": _numa_nodes(), "seconds_per_point": args.seconds,
                 "points": []}
    for channels in args.channels:
        for mode in args.modes:
            log(f"— {mode}, каналов {channels}...")
            if mode == "threads":
                res = run_threads(clip, channels, args.seconds)
            else:
                res = run_processes(clip, channels, args.seconds,
                                    pin=(mode == "processes"))
            out["points"].append(res)
            if not args.json:
                log("  " + json.dumps(res, ensure_ascii=False))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_table(out)
    return 0


def _cpu_model() -> str:
    """Модель процессора — без неё числа между прогонами несравнимы.

    Цикл 39 намерил на общих раннерах GitHub разброс 58 % на неизменном
    коде просто потому, что раннеры попадались с разными процессорами.
    """
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "неизвестно"


def _numa_nodes() -> int:
    try:
        return len([d for d in os.listdir("/sys/devices/system/node")
                    if d.startswith("node") and d[4:].isdigit()])
    except OSError:
        return 1


def _print_table(out: dict) -> None:
    print(f"CPU: {out['cpu']}, ядер {out['cpu_count']}, NUMA-нод {out['numa_nodes']}")
    print(f"{'режим':<20}{'кан.':>5}{'сумм. FPS':>11}{'FPS/канал':>11}"
          f"{'ядер':>7}{'FPS/ядро':>10}{'RSS МБ':>9}")
    for p in out["points"]:
        if p.get("error"):
            print(f"{p['mode']:<20}{p['channels']:>5}  ошибка: {p['error']}")
            continue
        print(f"{p['mode']:<20}{p['channels']:>5}{p['total_fps']:>11}"
              f"{p['fps_per_channel']:>11}{p['cores_busy']:>7}"
              f"{p['fps_per_core']:>10}{p['rss_mb'] or 0:>9}")


if __name__ == "__main__":
    sys.exit(main())
