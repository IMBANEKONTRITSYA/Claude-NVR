#!/usr/bin/env python3
"""Замер §19 «Запись: remux без перекодирования, CPU ≤ 0.04 ядра/камера».

Отдельный файл от `perf/bench.py`, а не группа в нём: тот меряет чистые
вычисления в одном процессе, а здесь нужен запущенный MediaMTX, N
публикующих ffmpeg и свободные порты — набор требований, из-за которого
группа никогда не пошла бы в набор по умолчанию.

**Зачем.** §19 задаёт удельную стоимость слоя записи, и на этой же вилке
§16 («0.02-0.04 ядра на камеру») стоит вся автоконфигурация
(`backend/app/services/autoconfig.py: REC_CORES_PER_CAMERA`). До цикла 32
число ни разу не измерялось — бралось из ТЗ как данность, то есть
предложение автоконфигурации опиралось на непроверенную константу.

**Схема.** Настоящий MediaMTX с конфигурацией записи из
`worker/record_layer.py:path_conf()`, N публикующих ffmpeg в режиме
`-re -c copy` (камера отдаёт готовый H.264; перекодирования нет ни на
одной стороне — ровно то, что требует §19). Меряется CPU **только
процесса MediaMTX**: публикаторы играют роль камер, и их стоимость к
слою записи не относится.

**Два режима.** По умолчанию публикатор приходит *push*'ем. Продакшн так
не работает: воркер заводит путь с `source: rtsp://...` и
`sourceOnDemand: no`, то есть MediaMTX сам открывает соединение к камере
и держит его. Режим `--pull` воспроизводит именно это — публикатор
публикуется в путь `src{i}`, а записывающий путь `cam{i}` его тянет, беря
конфигурацию **из production-функции**, а не из копии.

Разница между режимами замерена (цикл 39, песочница, 720p H.264):

| Камер | push | pull (как в проде) |
|---|---|---|
| 1 | 0.0100 | 0.0190 |
| 4 | 0.0097 | 0.0158 |
| 8 | 0.0089 | 0.0138 |

То есть клиент RTSP добавляет ~55 % CPU на камеру при 8 камерах, и число
push-режима (единственное, что мерилось до цикла 39) занижало боевую
стоимость примерно в полтора раза. Норматив §19 выполняется в обоих
режимах, но сравнивать с ним надо pull.

**Чего замер не покрывает** (перепроверить на сервере, см.
`docs/DEPLOY_CHECKLIST.md` §6):

* Нет читателей live: HLS-мультиплексирование в стоимость не входит.
* Число камер ограничено песочницей; экстраполяция на 120-250 **завышает**:
  удельная стоимость убывает с числом камер (0.019 → 0.0138 при 1 → 8),
  то есть заметна постоянная часть. На настоящем массиве в неё вмешается
  диск.
* CPU песочницы сильнее целевого E5-2670 (нет AVX2, слабее single-thread),
  хотя remux — это копирование пакетов и запись, а не SIMD-арифметика.
* Камера отдаёт синтетический поток; битрейт объекта выше.

Запуск (MediaMTX должен лежать рядом либо быть в PATH):

    python perf/bench_remux.py                 # push, 1, 4, 8, 16 камер
    python perf/bench_remux.py --pull 1 8      # как в проде
    python perf/bench_remux.py --pull --quick  # короткие окна (режим CI)
    MEDIAMTX_BIN=/usr/local/bin/mediamtx python perf/bench_remux.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from urllib import request

import psutil

WORK = os.environ.get("BENCH_REMUX_DIR") or os.path.join(
    tempfile.gettempdir(), "facewatch_remux_bench")
MEDIAMTX_BIN = (os.environ.get("MEDIAMTX_BIN")
                or shutil.which("mediamtx")
                or os.path.join(WORK, "mediamtx"))
API = "http://127.0.0.1:9997"
RTSP = "rtsp://127.0.0.1:8554"
SAMPLE = os.path.join(WORK, "sample720p.mp4")
SEGDIR = os.path.join(WORK, "bench_segments")


def make_sample():
    """720p H.264 15 fps ~2048 kbps — базовая конфигурация камеры (SPEC §1)."""
    os.makedirs(WORK, exist_ok=True)
    if os.path.exists(SAMPLE):
        return
    subprocess.run([
        "ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15",
        "-t", "30", "-c:v", "libx264", "-preset", "veryfast", "-b:v", "2048k",
        "-maxrate", "2048k", "-bufsize", "4096k", "-g", "30", "-pix_fmt", "yuv420p",
        SAMPLE,
    ], check=True, capture_output=True)


def mediamtx_conf():
    """Конфигурация без путей: пути заводятся через Control API, как в проде."""
    return f"""
logLevel: error
api: yes
apiAddress: 127.0.0.1:9997
rtsp: yes
rtspAddress: :8554
rtmp: no
hls: no
webrtc: no
srt: no
pathDefaults:
  sourceOnDemand: no
paths: {{}}
"""


def api_post(path, payload):
    req = request.Request(f"{API}{path}", data=json.dumps(payload).encode(),
                          headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=10) as r:
        return r.status


def path_conf():
    """Та же конфигурация, что worker/record_layer.py:path_conf(), но без
    `source` — публикатор приходит сам (push), а не тянется с камеры.
    Работа MediaMTX по записи от этого не меняется: тот же remux в fmp4."""
    return {
        "record": True,
        "recordFormat": "fmp4",
        "recordPath": f"{SEGDIR}/%path_%s",
        "recordSegmentDuration": "5m",
        "recordDeleteAfter": "0s",
    }


def pull_path_conf(source: str) -> dict:
    """**Продакшн**-конфигурация пути: та, что уходит в MediaMTX из воркера.

    Берётся из `worker/record_layer.py:path_conf()`, а не переписывается
    здесь: копия разошлась бы с боевой, и замер мерил бы не то, что поедет
    на объект (тот же урок, что в `test_archive_bench_matches_model.py` —
    бенчмарк обязан импортировать production-функцию, а не повторять её).

    Отличие от `path_conf()` выше и есть предмет режима `--pull`: MediaMTX
    сам открывает RTSP-соединение к камере и держит его (`sourceOnDemand:
    no`). Это дополнительная работа — клиент RTSP, разбор потока, реконнект,
    — которой при push-публикации нет вовсе, и до цикла 39 она в число
    §19/§16 не входила, хотя на объекте есть всегда.
    """
    sys.path.insert(0, os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "worker"))
    from record_layer import path_conf as production_path_conf  # noqa: PLC0415

    conf = production_path_conf(source, segment_duration_min=5,
                                media_root=os.path.dirname(SEGDIR))
    # Каталог замера — не `<media>/segments`: SEGDIR задаётся отдельно,
    # чтобы бенчмарк не писал в архив песочницы.
    conf["recordPath"] = f"{SEGDIR}/%path_%s"
    return conf


def run(n_cameras: int, warmup: int, measure: int, pull: bool = False) -> dict:
    shutil.rmtree(SEGDIR, ignore_errors=True)
    os.makedirs(SEGDIR, exist_ok=True)
    conf = os.path.join(WORK, "mediamtx_bench.yml")
    open(conf, "w").write(mediamtx_conf())

    mtx = subprocess.Popen([MEDIAMTX_BIN, conf],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    proc = psutil.Process(mtx.pid)

    pubs = []
    try:
        # В режиме pull публикатор играет камеру: он публикуется в путь
        # `src{i}`, а записывающий путь `cam{i}` ТЯНЕТ его — ровно так, как
        # воркер заводит настоящую камеру. Путь-источник записи не ведёт,
        # иначе в стоимость попала бы вторая запись того же потока.
        publish_to = "src" if pull else "cam"
        for i in range(n_cameras):
            if pull:
                api_post(f"/v3/config/paths/add/src{i}",
                         {"source": "publisher", "record": False})
                api_post(f"/v3/config/paths/add/cam{i}",
                         pull_path_conf(f"{RTSP}/src{i}"))
            else:
                api_post(f"/v3/config/paths/add/cam{i}", path_conf())
        for i in range(n_cameras):
            pubs.append(subprocess.Popen([
                "ffmpeg", "-re", "-stream_loop", "-1", "-i", SAMPLE,
                "-c", "copy", "-f", "rtsp", f"{RTSP}/{publish_to}{i}",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.15)

        time.sleep(warmup)
        # Публикаторы должны реально идти, иначе замеряется простой.
        with request.urlopen(f"{API}/v3/paths/list", timeout=10) as r:
            paths = json.load(r)
        # Считаются только записывающие пути: в режиме pull `src{i}` тоже
        # ready, и без фильтра «готово» вышло бы вдвое больше камер, чем
        # есть, а замер выглядел бы вдвое дешевле на камеру.
        ready = sum(1 for p in paths["items"]
                    if p.get("ready") and str(p.get("name", "")).startswith("cam"))

        proc.cpu_percent(None)          # первый вызов задаёт базу отсчёта
        rss_before = proc.memory_info().rss
        t0 = time.time()
        time.sleep(measure)
        cpu = proc.cpu_percent(None)    # % одного ядра, суммарно по нитям
        elapsed = time.time() - t0
        rss = proc.memory_info().rss

        size = sum(os.path.getsize(os.path.join(SEGDIR, f))
                   for f in os.listdir(SEGDIR)) if os.path.isdir(SEGDIR) else 0
        files = len(os.listdir(SEGDIR)) if os.path.isdir(SEGDIR) else 0
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

    return {
        "cameras": n_cameras,
        "mode": "pull" if pull else "push",
        "ready": ready,
        "cpu_percent_total": round(cpu, 2),
        "cores_total": round(cpu / 100.0, 4),
        "cores_per_camera": round(cpu / 100.0 / n_cameras, 4),
        "rss_mb": round(rss / 2**20, 1),
        "rss_mb_per_camera": round((rss - rss_before) / 2**20 / n_cameras, 2),
        "rss_mb_total_per_camera": round(rss / 2**20 / n_cameras, 2),
        "segment_files": files,
        "written_mb": round(size / 2**20, 1),
        "measure_sec": round(elapsed, 1),
    }


if __name__ == "__main__":
    if not os.path.exists(MEDIAMTX_BIN):
        sys.exit(f"MediaMTX не найден: {MEDIAMTX_BIN}. "
                 "Укажите путь через MEDIAMTX_BIN=/path/to/mediamtx.")
    args = [a for a in sys.argv[1:]]
    # --pull: продакшн-конфигурация (MediaMTX сам тянет камеру). Именно она
    # едет на объект, и до цикла 39 её стоимость не мерялась — замер шёл
    # только по push-публикации, у которой нет клиента RTSP.
    pull = "--pull" in args
    quick = "--quick" in args        # короткие окна: для джобы CI
    counts = [int(a) for a in args if not a.startswith("-")] or [1, 4, 8, 16]
    warmup, measure = (5, 10) if quick else (12, 30)

    make_sample()
    out = []
    for n in counts:
        r = run(n, warmup=warmup, measure=measure, pull=pull)
        print(json.dumps(r, ensure_ascii=False))
        out.append(r)
        assert r["ready"] == n, (
            f"готовы {r['ready']} из {n} записывающих путей — замер шёл "
            f"по простаивающим камерам и его нельзя засчитывать")
    out_path = os.path.join(WORK, "remux_bench.json")
    open(out_path, "w").write(json.dumps(out, indent=2))
    worst = max(r["cores_per_camera"] for r in out)
    print(f"\nНорматив §19: CPU ≤ 0.04 ядра/камера. Режим: "
          f"{'pull (как в проде)' if pull else 'push'}. "
          f"Максимум по прогону: {worst} ядра/камеру.")
    print(f"Результаты: {out_path}")
    if worst > 0.04:
        print("ВНИМАНИЕ: норматив §19 не выполняется на этом железе")
