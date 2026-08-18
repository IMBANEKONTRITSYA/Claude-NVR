#!/usr/bin/env python3
"""Два слоя под нагрузкой одновременно: §19 «CPU ≤ 80 %» и §2 «слои независимы».

**Чего не хватало.** Все замеры проекта до цикла 41 меряли слои по
отдельности: `bench_remux.py` — стоимость записи при простаивающей
аналитике, `bench.py --only chain` и `bench_scaling.py` — стоимость
аналитики при отсутствующей записи. На объекте оба слоя работают
**одновременно и на одной машине**, и ровно об этом два утверждения ТЗ,
которые из раздельных замеров не следуют:

* §19 «CPU ≤ 80 % при полной нагрузке (запас 20 %)» — величина про
  машину целиком, а не про процесс;
* §2 «Отказ аналитики НЕ влияет на запись, и наоборот» — заявлена
  независимость слоёв. Отказ проверялся (остановкой воркера), а
  **деградация под нагрузкой** — нет: вопрос не «переживёт ли запись
  падение аналитики», а «не начнёт ли запись терять сегменты, когда
  аналитика займёт процессор».

Замер идёт в две фазы на одном и том же наборе камер:

1. **только запись** — база: CPU машины, CPU медиасервера, темп прироста
   сегментов;
2. **запись + аналитика** — те же величины, плюс FPS каналов.

Сравнение фаз и есть ответ. Если §2 выполняется, во второй фазе у слоя
записи не должно измениться ни удельное CPU, ни темп сегментов; если
аналитика вытесняет запись — это будет видно как просадка темпа или
пропуск сегмента.

**Длительность сегмента здесь 10 с, а не 5 минут (§20).** Непрерывность
проверяется по приросту файлов, и на боевой длительности окно замера
пришлось бы растянуть на десятки минут ради двух точек. Короткий сегмент
строго чувствительнее: ротаций больше, пропуск заметнее. Всё остальное в
конфигурации пути — production'ное, из `worker/record_layer.py`.

**Масштаб.** Песочница — 4 ядра, «полная нагрузка объекта» (§20: 128–256+
камер) на ней не воспроизводима физически. Поэтому замер берёт нагрузку,
которую машина держит, а к нормативу §19 приводит **удельными** числами:
ядро на камеру записи и ядро на канал аналитики, замеренные **в
совместном режиме**, а не по отдельности. Экстраполяция печатается явно и
помечена как экстраполяция.

Запуск:

    MEDIAMTX_BIN=/путь/mediamtx python perf/bench_load.py
    MEDIAMTX_BIN=... python perf/bench_load.py --cameras 8 --analytics 2 --json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from urllib import request

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_ROOT, "worker"))

import bench_remux as remux                                   # noqa: E402

# §19: детекция ≥ 5 FPS/канал; CPU ≤ 80 % при полной нагрузке.
DETECTION_FPS_TARGET = 5.0
CPU_BUDGET_PCT = 80.0

# Длительность сегмента замера — см. докстринг.
SEGMENT_SEC = 10


def _segment_stats(segdir: str) -> tuple[int, int]:
    """Сколько файлов сегментов и сколько в них байт."""
    files = 0
    size = 0
    for name in os.listdir(segdir):
        path = os.path.join(segdir, name)
        try:
            size += os.path.getsize(path)
        except OSError:
            continue
        files += 1
    return files, size


def _load_path_conf(source: str, segdir: str) -> dict:
    """Продакшн-конфигурация пути с укороченным сегментом.

    Берётся из `worker/record_layer.py`, а не переписывается: копия
    разошлась бы с боевой, и замер мерил бы не то, что поедет на объект.
    """
    from record_layer import path_conf as production_path_conf

    conf = production_path_conf(source, segment_duration_min=5,
                                media_root=os.path.dirname(segdir))
    conf["recordPath"] = f"{segdir}/%path_%s"
    conf["recordSegmentDuration"] = f"{SEGMENT_SEC}s"
    return conf


class _Sampler:
    """Замер CPU машины и одного процесса за окно."""

    def __init__(self, proc):
        import psutil
        self.psutil = psutil
        self.proc = proc

    def __enter__(self):
        self.psutil.cpu_percent(interval=None)
        self.proc.cpu_percent(interval=None)
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.t0
        # cpu_percent процесса нормирован на ОДНО ядро (может быть > 100),
        # системный — на машину целиком (0..100). Разные шкалы намеренно:
        # §19 задаёт потолок машины, §16 — стоимость на камеру в ядрах.
        self.machine_pct = self.psutil.cpu_percent(interval=None)
        self.proc_cores = self.proc.cpu_percent(interval=None) / 100.0
        return False


def _analytics_channels(clip: str, count: int, seconds: float) -> dict:
    """Поднять `count` каналов аналитики нитями — как в worker.manager()."""
    import bench_scaling
    return bench_scaling.run_threads(clip, count, seconds)


def run(cameras: int, analytics: int, warmup: int, measure: int) -> dict:
    import psutil

    if not remux.MEDIAMTX_BIN or not os.path.exists(remux.MEDIAMTX_BIN):
        return {"skipped": "нет MEDIAMTX_BIN — нужен настоящий MediaMTX"}
    if not shutil.which("ffmpeg"):
        return {"skipped": "нет ffmpeg"}

    segdir = remux.SEGDIR
    remux.make_sample()

    # Клип аналитики готовится ДО старта медиасервера: кодирование H.265
    # занимает машину целиком и исказило бы базовую фазу, попади оно в окно.
    import bench
    clip = os.path.join(remux.WORK, "faces_load.mp4")
    if not os.path.exists(clip):
        made = bench.make_face_clip(clip)
        if made.get("error"):
            return {"error": made["error"]}

    shutil.rmtree(segdir, ignore_errors=True)
    os.makedirs(segdir, exist_ok=True)
    conf = os.path.join(remux.WORK, "mediamtx_load.yml")
    with open(conf, "w") as fh:
        fh.write(remux.mediamtx_conf())

    mtx = subprocess.Popen([remux.MEDIAMTX_BIN, conf],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    mtx_proc = psutil.Process(mtx.pid)
    pubs: list[subprocess.Popen] = []
    out: dict = {"cameras": cameras, "analytics_channels": analytics,
                 "cpu": _cpu_model(), "cpu_count": os.cpu_count(),
                 "segment_sec": SEGMENT_SEC}
    try:
        # Продакшн-раскладка: путь-источник играет камеру, записывающий
        # путь её ТЯНЕТ (pull) — как воркер заводит настоящую камеру.
        for i in range(cameras):
            remux.api_post(f"/v3/config/paths/add/src{i}",
                           {"source": "publisher", "record": False})
            remux.api_post(f"/v3/config/paths/add/cam{i}",
                           _load_path_conf(f"{remux.RTSP}/src{i}", segdir))
        for i in range(cameras):
            pubs.append(subprocess.Popen([
                "ffmpeg", "-re", "-stream_loop", "-1", "-i", remux.SAMPLE,
                "-c", "copy", "-f", "rtsp", f"{remux.RTSP}/src{i}",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.15)

        time.sleep(warmup)
        with request.urlopen(f"{remux.API}/v3/paths/list", timeout=10) as r:
            paths = json.load(r)
        ready = sum(1 for p in paths["items"]
                    if p.get("ready") and str(p.get("name", "")).startswith("cam"))
        if ready != cameras:
            return {"error": f"готовы {ready} из {cameras} записывающих путей"}

        # --- фаза 1: только запись ---------------------------------------
        files0, bytes0 = _segment_stats(segdir)
        with _Sampler(mtx_proc) as s1:
            time.sleep(measure)
        files1, bytes1 = _segment_stats(segdir)
        out["record_only"] = _phase(s1, cameras, files1 - files0,
                                    bytes1 - bytes0)

        # --- фаза 2: запись + аналитика ----------------------------------
        # Аналитика идёт нитями с общей моделью — той же конструкцией, что
        # в worker.manager(); замер её FPS отдаёт bench_scaling, чтобы
        # цепочка здесь не повторялась второй копией и не разошлась с ней.
        files2, bytes2 = _segment_stats(segdir)
        with _Sampler(mtx_proc) as s2:
            chain = _analytics_channels(clip, analytics, float(measure))
        files3, bytes3 = _segment_stats(segdir)
        combined = _phase(s2, cameras, files3 - files2, bytes3 - bytes2)
        combined["analytics"] = chain
        out["combined"] = combined

        out["verdict"] = _verdict(out["record_only"], combined, cameras, analytics)
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
    return out


def _phase(sampler, cameras: int, new_files: int, new_bytes: int) -> dict:
    return {
        "wall_sec": round(sampler.wall, 2),
        "machine_cpu_pct": round(sampler.machine_pct, 1),
        "mediamtx_cores": round(sampler.proc_cores, 3),
        "record_cores_per_camera": round(sampler.proc_cores / cameras, 4) if cameras else None,
        "segments_written": new_files,
        # Темп записи — независимая от CPU проверка того, что слой записи не
        # просто «жив», а продолжает писать с той же скоростью. Байты, а не
        # файлы: файл может открыться и не наполниться.
        "archive_mb_per_sec": round(new_bytes / 1048576 / sampler.wall, 2)
        if sampler.wall else None,
    }


def _verdict(base: dict, combined: dict, cameras: int, analytics: int) -> dict:
    """Свести фазы к ответам на §19 и §2."""
    chain = combined.get("analytics") or {}
    base_rate = base.get("archive_mb_per_sec") or 0.0
    comb_rate = combined.get("archive_mb_per_sec") or 0.0
    rate_delta = ((comb_rate - base_rate) / base_rate * 100.0) if base_rate else None
    record_delta = None
    if base.get("record_cores_per_camera"):
        record_delta = ((combined["record_cores_per_camera"]
                         - base["record_cores_per_camera"])
                        / base["record_cores_per_camera"] * 100.0)
    return {
        # §19: потолок машины. Меряется, но на 4 ядрах песочницы это
        # потолок ПЕСОЧНИЦЫ под этой нагрузкой, а не объекта под полной.
        "machine_cpu_pct": combined["machine_cpu_pct"],
        "cpu_budget_pct": CPU_BUDGET_PCT,
        "meets_cpu_budget": combined["machine_cpu_pct"] <= CPU_BUDGET_PCT,
        # §2: слой записи под нагрузкой аналитики.
        "record_cores_per_camera_delta_pct": round(record_delta, 1)
        if record_delta is not None else None,
        "archive_rate_delta_pct": round(rate_delta, 1) if rate_delta is not None else None,
        # Просадка темпа архива больше 5 % — это уже не шум ротации, а
        # вытеснение записи аналитикой, то есть нарушение §2.
        "layers_independent": (rate_delta is None or rate_delta > -5.0),
        "segments_kept_growing": combined["segments_written"] > 0,
        # §19 на канал, но замеренный ПРИ РАБОТАЮЩЕЙ записи — до этого
        # замера число снималось на машине, где запись не шла.
        "analytics_fps_per_channel": chain.get("fps_per_channel"),
        "analytics_meets_target": chain.get("meets_target"),
        "detection_fps_target": DETECTION_FPS_TARGET,
        # Экстраполяция к объекту: удельные числа, замеренные СОВМЕСТНО.
        "extrapolation_note": (
            "ядер на объект = камеры × record_cores_per_camera + каналы × "
            "cores_per_camera_at_target; числа сняты в совместном режиме, "
            "но на 4 ядрах и с AVX2 — на E5-2670 перемерить"),
        "cores_per_analytics_channel": chain.get("cores_per_camera_at_target"),
    }


def _cpu_model() -> str:
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "неизвестно"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cameras", type=int, default=8, help="путей записи")
    ap.add_argument("--analytics", type=int, default=2, help="каналов аналитики")
    ap.add_argument("--warmup", type=int, default=12)
    ap.add_argument("--measure", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = run(args.cameras, args.analytics, args.warmup, args.measure)
    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if not res.get("error") else 1

    if res.get("skipped") or res.get("error"):
        print(res.get("skipped") or res["error"])
        return 0 if res.get("skipped") else 1
    print(f"CPU: {res['cpu']}, ядер {res['cpu_count']}")
    print(f"камер записи {res['cameras']}, каналов аналитики {res['analytics_channels']}")
    for name, key in (("только запись", "record_only"), ("запись + аналитика", "combined")):
        p = res[key]
        print(f"  {name:<22} CPU машины {p['machine_cpu_pct']:>5} % | "
              f"MediaMTX {p['mediamtx_cores']:>6} ядра "
              f"({p['record_cores_per_camera']} на камеру) | "
              f"архив {p['archive_mb_per_sec']} МБ/с | сегментов {p['segments_written']}")
    v = res["verdict"]
    print(f"  §19 CPU ≤ {v['cpu_budget_pct']} %: {v['machine_cpu_pct']} % — "
          f"{'да' if v['meets_cpu_budget'] else 'НЕТ'}")
    print(f"  §19 ≥ 5 FPS/канал при работающей записи: "
          f"{v['analytics_fps_per_channel']} — "
          f"{'да' if v['analytics_meets_target'] else 'НЕТ'}")
    print(f"  §2 слои независимы: темп архива {v['archive_rate_delta_pct']} %, "
          f"ядро/камеру {v['record_cores_per_camera_delta_pct']} % — "
          f"{'да' if v['layers_independent'] else 'НЕТ'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
