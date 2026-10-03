#!/usr/bin/env python3
"""Два слоя под нагрузкой одновременно: §19 «CPU ≤ 80 %», «RAM ≤ 80 %», §2 «слои независимы».

**Чего не хватало.** Все замеры проекта до цикла 41 меряли слои по
отдельности: `bench_remux.py` — стоимость записи при простаивающей
аналитике, `bench.py --only chain` и `bench_scaling.py` — стоимость
аналитики при отсутствующей записи. На объекте оба слоя работают
**одновременно и на одной машине**, и ровно об этом два утверждения ТЗ,
которые из раздельных замеров не следуют:

* §19 «CPU ≤ 80 % при полной нагрузке (запас 20 %)» — величина про
  машину целиком, а не про процесс;
* §19 «RAM ≤ 80 % от доступной (запас 20 %)» — вторая половина того же
  требования. До цикла 46 она не измерялась **ни разу ни одним замером
  проекта**: `bench_load.py` цикла 41 закрыл процессорную половину и
  оставил память вне поля зрения, а `bench_remux.py` считает RSS
  медиасервера, но не машины и не при работающей аналитике. Здесь
  меряются обе величины — потолок машины и удельная стоимость памяти на
  камеру записи и на канал аналитики (вилки §16);
* §2 «Отказ аналитики НЕ влияет на запись, и наоборот» — заявлена
  независимость слоёв. Отказ проверялся (остановкой воркера), а
  **деградация под нагрузкой** — нет: вопрос не «переживёт ли запись
  падение аналитики», а «не начнёт ли запись терять сегменты, когда
  аналитика займёт процессор».

Замер идёт в две фазы на одном и том же наборе камер:

1. **только запись** — база: CPU машины, CPU медиасервера, темп прироста
   сегментов;
2. **запись + аналитика** — те же величины, плюс FPS каналов.

Память снимается не одним чтением в конце окна, а **пиком** по выборкам
раз в полсекунды: потолок §19 — про худший момент, а не про тот, в
который замер случайно посмотрел. Ровно этот урок цикл 44 записал в
carryover («число, полученное одним прогоном, — не результат замера, а
его выборка»); отсюда же `--repeat`, который проверяет норматив по
**худшему** прогону, а не по последнему.

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
    MEDIAMTX_BIN=... python perf/bench_load.py --repeat 3      # норматив по худшему
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

# §19: детекция ≥ 5 FPS/канал; CPU ≤ 80 % и RAM ≤ 80 % при полной нагрузке.
DETECTION_FPS_TARGET = 5.0
CPU_BUDGET_PCT = 80.0
RAM_BUDGET_PCT = 80.0

# §16, вилки удельной памяти, на которых стоит калькулятор ресурсов
# (backend/app/services/autoconfig.py). Слой записи — «~50-100 MB на камеру
# (буферы)», слой аналитики — «~500 MB-2 GB на камеру (модель + буферы)».
REC_RAM_MB_PER_CAMERA = (50.0, 100.0)
ANALYTICS_RAM_MB_PER_CHANNEL = (500.0, 2048.0)

# Частота выборок памяти внутри окна. Потолок §19 — про худший момент;
# одно чтение в конце окна показало бы тот момент, в который замер
# случайно посмотрел.
MEM_SAMPLE_SEC = 0.5

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
    """Замер CPU и памяти машины плюс CPU/RSS отдельных процессов за окно.

    Память снимается фоновой нитью раз в `MEM_SAMPLE_SEC` и сводится
    **пиком**, а не последним значением: §19 задаёт потолок, то есть
    вопрос в худшем моменте окна. Нить дешёвая (три чтения /proc в
    полсекунды) и на измеряемую нагрузку не влияет — проверено тем, что
    базовая фаза без неё и с ней даёт одинаковое CPU в пределах шума.
    """

    def __init__(self, proc, self_proc=None):
        import psutil
        self.psutil = psutil
        self.proc = proc
        # Процесс самого замера: каналы аналитики крутятся его нитями (как
        # в worker.manager()), поэтому память слоя аналитики — это его RSS.
        self.self_proc = self_proc or psutil.Process()
        self._stop = None
        self._thread = None

    def _sample_loop(self):
        while not self._stop.wait(MEM_SAMPLE_SEC):
            self._take()

    def _take(self):
        vm = self.psutil.virtual_memory()
        self.machine_ram_pct = max(self.machine_ram_pct, vm.percent)
        self.machine_ram_used = max(self.machine_ram_used, vm.total - vm.available)
        for attr, proc in (("proc_rss", self.proc), ("self_rss", self.self_proc)):
            try:
                rss = proc.memory_info().rss
            except Exception:
                continue
            setattr(self, attr, max(getattr(self, attr), rss))

    def __enter__(self):
        import threading
        self.machine_ram_pct = 0.0
        self.machine_ram_used = 0
        self.proc_rss = 0
        self.self_rss = 0
        self.psutil.cpu_percent(interval=None)
        self.proc.cpu_percent(interval=None)
        self._take()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        self.wall = time.perf_counter() - self.t0
        self._stop.set()
        self._thread.join(timeout=MEM_SAMPLE_SEC * 4)
        self._take()
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
    mb = 1048576
    return {
        "wall_sec": round(sampler.wall, 2),
        "machine_cpu_pct": round(sampler.machine_pct, 1),
        "mediamtx_cores": round(sampler.proc_cores, 3),
        "record_cores_per_camera": round(sampler.proc_cores / cameras, 4) if cameras else None,
        # --- память (§19 «RAM ≤ 80 %», вилки §16) ------------------------
        # Пик за окно, а не значение на выходе из него.
        "machine_ram_pct": round(sampler.machine_ram_pct, 1),
        "machine_ram_used_mb": round(sampler.machine_ram_used / mb, 1),
        # RSS медиасервера целиком и на камеру. Второе число — то, что
        # §16 называет «~50-100 MB на камеру (буферы)»; там эта вилка
        # писалась под раскладку «процесс на камеру», а MediaMTX — один
        # процесс на все, поэтому расхождение ожидаемо и его надо видеть.
        "mediamtx_rss_mb": round(sampler.proc_rss / mb, 1),
        "record_rss_mb_per_camera": round(sampler.proc_rss / mb / cameras, 2) if cameras else None,
        # RSS процесса замера: каналы аналитики — его нити, значит это
        # память слоя аналитики вместе с моделью.
        "analytics_rss_mb": round(sampler.self_rss / mb, 1),
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
    # §16: удельная память. Аналитика считается ПРИРОСТОМ между фазами —
    # в базовой фазе процесс замера уже держит интерпретатор, psutil и
    # прочитанный клип, и записывать это в стоимость канала было бы
    # припиской. Модель у каналов общая (нити, как в worker.manager()),
    # поэтому число на канал заведомо ниже вилки §16, писавшейся под
    # «процесс на камеру» — это ожидаемое расхождение, а не находка.
    analytics_rss_delta = (combined.get("analytics_rss_mb", 0.0)
                           - base.get("analytics_rss_mb", 0.0))
    analytics_mb_per_channel = (round(analytics_rss_delta / analytics, 1)
                                if analytics else None)
    rec_mb = combined.get("record_rss_mb_per_camera")
    return {
        # §19: потолок машины. Меряется, но на 4 ядрах песочницы это
        # потолок ПЕСОЧНИЦЫ под этой нагрузкой, а не объекта под полной.
        "machine_cpu_pct": combined["machine_cpu_pct"],
        "cpu_budget_pct": CPU_BUDGET_PCT,
        "meets_cpu_budget": combined["machine_cpu_pct"] <= CPU_BUDGET_PCT,
        # §19, вторая половина: RAM ≤ 80 % от доступной. Та же оговорка про
        # масштаб, что и у CPU: на объекте камер больше, но и памяти
        # больше — переносимы отсюда только удельные числа ниже.
        "machine_ram_pct": combined["machine_ram_pct"],
        "ram_budget_pct": RAM_BUDGET_PCT,
        "meets_ram_budget": combined["machine_ram_pct"] <= RAM_BUDGET_PCT,
        "machine_ram_used_mb": combined["machine_ram_used_mb"],
        # §16: вилки, на которых стоит калькулятор ресурсов.
        "record_rss_mb_per_camera": rec_mb,
        "record_ram_bracket_mb": list(REC_RAM_MB_PER_CAMERA),
        "record_ram_within_bracket": (
            REC_RAM_MB_PER_CAMERA[0] <= rec_mb <= REC_RAM_MB_PER_CAMERA[1]
            if rec_mb is not None else None),
        "analytics_rss_mb_per_channel": analytics_mb_per_channel,
        "analytics_ram_bracket_mb": list(ANALYTICS_RAM_MB_PER_CHANNEL),
        "analytics_ram_within_bracket": (
            ANALYTICS_RAM_MB_PER_CHANNEL[0] <= analytics_mb_per_channel
            <= ANALYTICS_RAM_MB_PER_CHANNEL[1]
            if analytics_mb_per_channel is not None else None),
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
            "cores_per_camera_at_target; ГБ на объект = камеры × "
            "record_rss_mb_per_camera + каналы × analytics_rss_mb_per_channel "
            "+ база (модель, СУБД, Redis, nginx); числа сняты в совместном "
            "режиме, но на 4 ядрах и с AVX2 — на E5-2670 перемерить"),
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


def worst_verdict(verdicts: list[dict]) -> dict:
    """Свести вердикты нескольких прогонов к худшему — по каждой величине
    отдельно.

    Не «худший прогон целиком», а худшее значение каждого норматива: один
    прогон может дать пик CPU, другой — просадку темпа архива, и норматив,
    нарушенный хоть в одном, нарушен. Правило прямо из carryover цикла 44:
    число, полученное одним прогоном, — выборка, а не результат.
    """
    if len(verdicts) == 1:
        return dict(verdicts[0])
    out = dict(verdicts[-1])
    def worst(key, pick):
        vals = [v[key] for v in verdicts if v.get(key) is not None]
        return pick(vals) if vals else None
    out["machine_cpu_pct"] = worst("machine_cpu_pct", max)
    out["machine_ram_pct"] = worst("machine_ram_pct", max)
    out["machine_ram_used_mb"] = worst("machine_ram_used_mb", max)
    out["archive_rate_delta_pct"] = worst("archive_rate_delta_pct", min)
    out["record_cores_per_camera_delta_pct"] = worst("record_cores_per_camera_delta_pct", max)
    out["analytics_fps_per_channel"] = worst("analytics_fps_per_channel", min)
    out["record_rss_mb_per_camera"] = worst("record_rss_mb_per_camera", max)
    out["analytics_rss_mb_per_channel"] = worst("analytics_rss_mb_per_channel", max)
    # Булевы вердикты пересчитываются от худших чисел, а не берутся из
    # последнего прогона: иначе «худшее» осталось бы только в таблице.
    out["meets_cpu_budget"] = (out["machine_cpu_pct"] or 0) <= CPU_BUDGET_PCT
    out["meets_ram_budget"] = (out["machine_ram_pct"] or 0) <= RAM_BUDGET_PCT
    out["layers_independent"] = (out["archive_rate_delta_pct"] is None
                                 or out["archive_rate_delta_pct"] > -5.0)
    out["analytics_meets_target"] = (
        out["analytics_fps_per_channel"] is not None
        and out["analytics_fps_per_channel"] >= DETECTION_FPS_TARGET)
    out["segments_kept_growing"] = all(v.get("segments_kept_growing") for v in verdicts)
    out["runs"] = len(verdicts)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cameras", type=int, default=8, help="путей записи")
    ap.add_argument("--analytics", type=int, default=2, help="каналов аналитики")
    ap.add_argument("--warmup", type=int, default=12)
    ap.add_argument("--measure", type=int, default=25)
    ap.add_argument("--repeat", type=int, default=1,
                    help="прогонов; норматив проверяется по худшему")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    runs = []
    for _ in range(max(1, args.repeat)):
        r = run(args.cameras, args.analytics, args.warmup, args.measure)
        if r.get("skipped") or r.get("error"):
            res = r
            break
        runs.append(r)
    else:
        res = dict(runs[-1])
        res["verdict"] = worst_verdict([r["verdict"] for r in runs])
        if len(runs) > 1:
            res["runs"] = [{"machine_cpu_pct": r["verdict"]["machine_cpu_pct"],
                            "machine_ram_pct": r["verdict"]["machine_ram_pct"],
                            "archive_rate_delta_pct": r["verdict"]["archive_rate_delta_pct"],
                            "analytics_fps_per_channel": r["verdict"]["analytics_fps_per_channel"]}
                           for r in runs]

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
        print(f"  {'':<22} RAM машины {p['machine_ram_pct']:>5} % "
              f"({p['machine_ram_used_mb']} МБ) | MediaMTX {p['mediamtx_rss_mb']} МБ "
              f"({p['record_rss_mb_per_camera']} МБ на камеру) | "
              f"аналитика {p['analytics_rss_mb']} МБ")
    v = res["verdict"]
    if v.get("runs"):
        print(f"  прогонов: {v['runs']}, норматив проверяется по худшему")
    print(f"  §19 CPU ≤ {v['cpu_budget_pct']} %: {v['machine_cpu_pct']} % — "
          f"{'да' if v['meets_cpu_budget'] else 'НЕТ'}")
    print(f"  §19 RAM ≤ {v['ram_budget_pct']} %: {v['machine_ram_pct']} % "
          f"({v['machine_ram_used_mb']} МБ) — "
          f"{'да' if v['meets_ram_budget'] else 'НЕТ'}")
    lo, hi = v["record_ram_bracket_mb"]
    print(f"  §16 запись {lo}-{hi} МБ/камеру: {v['record_rss_mb_per_camera']} МБ — "
          f"{'в вилке' if v['record_ram_within_bracket'] else 'вне вилки'}")
    lo, hi = v["analytics_ram_bracket_mb"]
    print(f"  §16 аналитика {lo}-{hi} МБ/канал: {v['analytics_rss_mb_per_channel']} МБ — "
          f"{'в вилке' if v['analytics_ram_within_bracket'] else 'вне вилки'}")
    print(f"  §19 ≥ 5 FPS/канал при работающей записи: "
          f"{v['analytics_fps_per_channel']} — "
          f"{'да' if v['analytics_meets_target'] else 'НЕТ'}")
    print(f"  §2 слои независимы: темп архива {v['archive_rate_delta_pct']} %, "
          f"ядро/камеру {v['record_cores_per_camera_delta_pct']} % — "
          f"{'да' if v['layers_independent'] else 'НЕТ'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
