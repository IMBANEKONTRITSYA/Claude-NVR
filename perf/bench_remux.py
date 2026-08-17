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

**Чего замер не покрывает** (перепроверить на сервере, см.
`docs/DEPLOY_CHECKLIST.md` §6):

* Публикаторы приходят *push*, а продакшн-конфигурация *pull*'ит камеру
  (`source: rtsp://...`, `sourceOnDemand: no`). Клиент RTSP на камеру —
  дополнительная стоимость, которой здесь нет.
* Нет читателей live: HLS-мультиплексирование в стоимость не входит.
* Число камер ограничено песочницей; экстраполяция на 120-250 линейна по
  построению, а на настоящем массиве в неё вмешается диск.
* CPU песочницы сильнее целевого E5-2670 (нет AVX2, слабее single-thread),
  хотя remux — это копирование пакетов и запись, а не SIMD-арифметика.

Запуск (MediaMTX должен лежать рядом либо быть в PATH):

    python perf/bench_remux.py                 # 1, 4, 8, 16 камер
    python perf/bench_remux.py 1 8 32          # свой набор
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


def run(n_cameras: int, warmup: int, measure: int) -> dict:
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
        for i in range(n_cameras):
            api_post(f"/v3/config/paths/add/cam{i}", path_conf())
        for i in range(n_cameras):
            pubs.append(subprocess.Popen([
                "ffmpeg", "-re", "-stream_loop", "-1", "-i", SAMPLE,
                "-c", "copy", "-f", "rtsp", f"{RTSP}/cam{i}",
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
            time.sleep(0.15)

        time.sleep(warmup)
        # Публикаторы должны реально идти, иначе замеряется простой.
        with request.urlopen(f"{API}/v3/paths/list", timeout=10) as r:
            paths = json.load(r)
        ready = sum(1 for p in paths["items"] if p.get("ready"))

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
    make_sample()
    out = []
    for n in (int(a) for a in sys.argv[1:] or ["1", "4", "8", "16"]):
        r = run(n, warmup=12, measure=30)
        print(json.dumps(r, ensure_ascii=False))
        out.append(r)
    out_path = os.path.join(WORK, "remux_bench.json")
    open(out_path, "w").write(json.dumps(out, indent=2))
    print(f"\nНорматив §19: CPU ≤ 0.04 ядра/камера. "
          f"Максимум по прогону: {max(r['cores_per_camera'] for r in out)} ядра/камеру.")
    print(f"Результаты: {out_path}")
