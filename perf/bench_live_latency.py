#!/usr/bin/env python3
"""Замер §19/§4 «задержка live ≤ 3 секунд» — вся цепочка, до картинки в браузере.

**Почему норматив висел незакрытым.** Цикл 33 померил серверную часть —
160 мс от кадра, отданного источником, до сегмента, отданного MediaMTX, —
и записал в carryover, что «2 из 3 слагаемых требуют браузера и камеры».
Слагаемых действительно три:

1. кодирование и доставка от камеры в MediaMTX (RTSP);
2. упаковка в LL-HLS и отдача сегментов/частей — это и мерил цикл 33;
3. **буфер и декод в самом браузере** — а это, по опыту HLS, самое
   большое слагаемое из трёх, и именно оно не мерилось ни разу.

Норматив ТЗ сформулирован про то, что видит оператор («задержка ≤ 3
секунд», §4 и §19), то есть про сумму всех трёх. Мерить нужно её.

**Что здесь меряется.** Полная цепочка от события перед объективом до
пикселя на экране:

    ffmpeg (H.264, zerolatency)  --RTSP-->  MediaMTX  --LL-HLS-->
        Chromium + hls.js (тот же, что во фронтенде)  -->  <video>

**Как ловится момент «видно на экране» без OCR.** Источник даёт не
таймкод, а **мигание**: кадр целиком переключается между чёрным и белым
по расписанию `enable='lt(mod(t,PERIOD),PERIOD/2)'`. В браузере страница
на каждом кадре (`requestVideoFrameCallback`) сносит `<video>` в canvas
1×1 и запоминает момент, когда средняя яркость пересекла середину.
Разница «когда переход стал виден» минус «когда переход был снят» и есть
задержка. Никакого распознавания текста, никакой привязки к частоте
опроса: `requestVideoFrameCallback` вызывается ровно на предъявленных
кадрах и отдаёт их `mediaTime`.

**Как стыкуются часы источника и браузера.** Обе стороны живут в одном
процессе-хосте, поэтому общие часы — системные:

* браузер отдаёт эпохальное время как `performance.timeOrigin +
  performance.now()`;
* у ffmpeg берётся `-progress pipe:1`: рядом с каждой строкой
  `out_time_us` хост ставит своё `time.time()`. Медиана разностей даёт
  момент, в который источник снял кадр с временем потока t=0. `-re`
  держит темп реального времени, так что дальше t переводится в
  эпохальное время линейно; медиана по многим отсчётам гасит джиттер
  отдельной строки.

**Что честно завышено.** Кодирует ffmpeg на том же хосте, а на объекте
кодирует сама камера. Latency `libx264 -tune zerolatency` на 720p — это
единицы миллисекунд, и они входят в результат; на объекте вместо них
будет задержка кодера камеры, обычно больше. То есть замер здесь — это
нижняя граница по слагаемому 1 и настоящая величина по слагаемым 2-3,
которые и не мерились. Сеть между камерой и сервером (LAN) здесь тоже
отсутствует. Всё это записано в отчёт как «перепроверить на сервере».

Запуск:

    python perf/bench_live_latency.py                  # 8 переходов
    python perf/bench_live_latency.py --transitions 16
    python perf/bench_live_latency.py --json

Требуется: ffmpeg, бинарник MediaMTX (--mediamtx или $MEDIAMTX_BIN),
Chromium с Playwright, собранный hls.js (frontend/node_modules).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Норматив ТЗ: §4 «Задержка ≤ 3 секунд», §19 «Задержка live ≤ 3 секунд».
NORMATIVE_SEC = 3.0

# Период мигания. Полупериод обязан превышать измеряемую задержку:
# сопоставление идёт «последний переход того же цвета не позже показа»,
# и на задержке больше полупериода оно попало бы в предыдущее мигание и
# напечатало бы заниженное число. 8 с при нормативе 3 с дают полупериод
# 4 с — весь диапазон норматива разрешается однозначно, с запасом.
FLASH_PERIOD_SEC = 8.0

STREAM_PATH = "benchlive"


def _free_ports(n: int) -> list[int]:
    """Свободные порты одной пачкой: в песочнице и на раннере штатные
    8554/8888 могут быть заняты, а падать из-за этого замер не должен."""
    socks, ports = [], []
    for _ in range(n):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        ports.append(s.getsockname()[1])
        socks.append(s)
    for s in socks:
        s.close()
    return ports


def _wait_port(port: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


# --------------------------------------------------------------- MediaMTX

MTX_CONF = """
logLevel: warn
rtspAddress: :{rtsp}
rtmp: no
srt: no
webrtc: no
hls: yes
hlsAddress: :{hls}
hlsAlwaysRemux: yes
hlsVariant: lowLatency
hlsSegmentCount: {seg_count}
hlsSegmentDuration: {seg_dur}
hlsPartDuration: {part_dur}
hlsAllowOrigin: '*'
api: no
metrics: no
pprof: no
playback: no
paths:
  {path}:
"""


def _hls_settings() -> dict[str, str]:
    """Параметры LL-HLS берутся из боевого mediamtx/mediamtx.yml, а не
    пишутся здесь заново: замер обязан мерить ту же настройку, которая
    поедет на объект. Разошлись бы — мерили бы чужой профиль буферизации."""
    conf = (REPO / "mediamtx" / "mediamtx.yml").read_text(encoding="utf-8")
    out = {}
    for key, default in (("hlsSegmentCount", "7"),
                         ("hlsSegmentDuration", "1s"),
                         ("hlsPartDuration", "200ms")):
        for line in conf.splitlines():
            line = line.strip()
            if line.startswith(key + ":"):
                out[key] = line.split(":", 1)[1].strip()
                break
        else:
            out[key] = default
    return out


# ----------------------------------------------------------------- ffmpeg

class Publisher:
    """ffmpeg, публикующий мигающий поток, плюс привязка его шкалы к
    системным часам по `-progress`."""

    def __init__(self, rtsp_port: int, fps: int, duration: float):
        self.rtsp_port = rtsp_port
        self.fps = fps
        self.duration = duration
        self.proc: subprocess.Popen | None = None
        self._offsets: list[float] = []
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        # drawbox поверх чёрного источника: полкадра периода — белое поле,
        # полкадра — чёрное. gte(...) даёт ровно один переход на полпериода.
        vf = (f"drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:"
              f"enable='lt(mod(t\\,{FLASH_PERIOD_SEC})\\,{FLASH_PERIOD_SEC / 2})'")
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-re",  # темп реального времени — без него шкала t не время
            "-f", "lavfi",
            "-i", f"color=c=black:s=1280x720:r={self.fps}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
            # GOP в одну секунду: LL-HLS нарезает части по ключевым кадрам,
            # и редкие ключевые кадры сами по себе стали бы задержкой.
            "-g", str(self.fps), "-keyint_min", str(self.fps),
            "-pix_fmt", "yuv420p", "-b:v", "2M",
            "-t", str(self.duration),
            "-f", "rtsp", "-rtsp_transport", "tcp",
            f"rtsp://127.0.0.1:{self.rtsp_port}/{STREAM_PATH}",
            "-progress", "pipe:1", "-nostats",
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self._thread = threading.Thread(target=self._read_progress, daemon=True)
        self._thread.start()

    def _read_progress(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            line = line.strip()
            if not line.startswith("out_time_us="):
                continue
            now = time.time()
            raw = line.split("=", 1)[1]
            if not raw.lstrip("-").isdigit():
                continue
            out_time = int(raw) / 1e6
            if out_time <= 0:
                continue
            # Момент системных часов, соответствующий t=0 в потоке.
            self._offsets.append(now - out_time)

    @property
    def samples(self) -> int:
        return len(self._offsets)

    def epoch_of_stream_time(self, t: float) -> float:
        """Системное время, когда источник снял кадр со шкалой t."""
        if not self._offsets:
            raise RuntimeError("ffmpeg не отдал ни одной строки -progress")
        return statistics.median(self._offsets) + t

    def transitions(self) -> list[tuple[float, str]]:
        """Переходы яркости на шкале потока: (t, 'white'|'black')."""
        out: list[tuple[float, str]] = []
        k = 0
        while True:
            t = k * FLASH_PERIOD_SEC / 2
            if t > self.duration:
                break
            # enable=lt(mod(t,P), P/2): белое на [0,P/2), чёрное на [P/2,P)
            out.append((t, "white" if k % 2 == 0 else "black"))
            k += 1
        return out

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------- браузер

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>bench</title>
<script src="/hls.js"></script></head>
<body style="margin:0;background:#888">
<video id="v" muted playsinline style="width:640px;height:360px"></video>
<script>
window.__events = [];
window.__err = null;
const v = document.getElementById('v');
const c = document.createElement('canvas');
c.width = 16; c.height = 16;
const ctx = c.getContext('2d', {willReadFrequently: true});
let last = null;

// Момент предъявления кадра берём у самого браузера, а не у таймера:
// requestVideoFrameCallback зовётся на КАДРАХ, которые ушли на экран, и
// приносит их presentation time. Опрос по setInterval добавил бы к
// результату собственный шаг и мерил бы опрос, а не задержку.
function onFrame(now, meta) {
  try {
    ctx.drawImage(v, 0, 0, c.width, c.height);
    const d = ctx.getImageData(0, 0, c.width, c.height).data;
    let sum = 0;
    for (let i = 0; i < d.length; i += 4) sum += d[i];
    const lum = sum / (d.length / 4);
    const state = lum > 128 ? 'white' : 'black';
    if (last !== null && state !== last) {
      window.__events.push({
        state: state,
        // presentationTime — в шкале performance; переводим в эпохальную
        // тем же timeOrigin, что и на хосте.
        epoch: performance.timeOrigin + (meta.presentationTime || now),
        mediaTime: meta.mediaTime,
      });
    }
    last = state;
  } catch (e) { window.__err = String(e); }
  v.requestVideoFrameCallback(onFrame);
}

const hls = new Hls({
  // Ровно тот профиль, что во фронтенде (LiveGrid.tsx): низкая задержка,
  // иначе мерили бы буфер по умолчанию, а не тот, что у оператора.
  lowLatencyMode: true,
  liveSyncDurationCount: window.__syncCount,
  backBufferLength: 10,
});
hls.on(Hls.Events.ERROR, (e, data) => {
  if (data.fatal) window.__err = data.type + '/' + data.details;
});
hls.loadSource(window.__src);
hls.attachMedia(v);
hls.on(Hls.Events.MANIFEST_PARSED, () => {
  v.play().then(() => { v.requestVideoFrameCallback(onFrame); })
          .catch(e => { window.__err = 'play: ' + e; });
});
</script></body></html>"""


def _serve_page(port: int, hlsjs: Path, src: str,
                sync_count: int) -> subprocess.Popen:
    """Отдельный http-сервер для страницы: hls.js обязан приехать с того
    же origin, а file:// в Chromium не даёт fetch к localhost."""
    root = Path(tempfile.mkdtemp(prefix="benchlive-"))
    (root / "index.html").write_text(
        PAGE.replace("window.__src", json.dumps(src))
            .replace("window.__syncCount", str(sync_count)), encoding="utf-8")
    shutil.copy(hlsjs, root / "hls.js")
    return subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "-b", "127.0.0.1"],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _find_hlsjs() -> Path:
    """hls.js — тот же, что у фронтенда. Копия из CDN мерила бы чужую
    версию с другим поведением буфера."""
    for rel in ("frontend/node_modules/hls.js/dist/hls.min.js",
                "frontend/node_modules/hls.js/dist/hls.js"):
        p = REPO / rel
        if p.exists():
            return p
    raise SystemExit(
        "не найден hls.js — выполните `npm install` в frontend/ "
        "(замер обязан использовать ту же версию плеера, что и интерфейс)")


def _find_mediamtx(explicit: str | None) -> str:
    for cand in (explicit, os.environ.get("MEDIAMTX_BIN"),
                 shutil.which("mediamtx")):
        if cand and Path(cand).exists():
            return cand
    raise SystemExit(
        "не найден бинарник MediaMTX: укажите --mediamtx или $MEDIAMTX_BIN "
        "(версия — из docker-compose.yml, sha256 — из packaging/versions.env)")


# ------------------------------------------------------------------ замер

def measure(args: argparse.Namespace) -> dict:
    from playwright.sync_api import sync_playwright

    mtx_bin = _find_mediamtx(args.mediamtx)
    hlsjs = _find_hlsjs()
    hls_cfg = _hls_settings()

    rtsp_port, hls_port, page_port = _free_ports(3)
    conf = MTX_CONF.format(
        rtsp=rtsp_port, hls=hls_port, path=STREAM_PATH,
        seg_count=hls_cfg["hlsSegmentCount"],
        seg_dur=hls_cfg["hlsSegmentDuration"],
        part_dur=hls_cfg["hlsPartDuration"])
    conf_path = Path(tempfile.mkdtemp(prefix="benchmtx-")) / "mediamtx.yml"
    conf_path.write_text(conf, encoding="utf-8")

    # Длительность потока: разгон плеера + запрошенные переходы + хвост.
    duration = args.warmup + (args.transitions + 1) * FLASH_PERIOD_SEC / 2 + 4

    mtx = subprocess.Popen([mtx_bin, str(conf_path)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pub = Publisher(rtsp_port, args.fps, duration)
    page_srv = None
    try:
        if not _wait_port(rtsp_port):
            raise SystemExit("MediaMTX не поднял RTSP-порт")
        if not _wait_port(hls_port):
            raise SystemExit("MediaMTX не поднял HLS-порт")

        pub.start()
        src = f"http://127.0.0.1:{hls_port}/{STREAM_PATH}/index.m3u8"
        page_srv = _serve_page(page_port, hlsjs, src, args.sync_count)
        if not _wait_port(page_port):
            raise SystemExit("не поднялся http-сервер страницы")

        with sync_playwright() as p:
            # Полный Chromium, а не headless-shell: HLS здесь несёт H.264,
            # и урезанная сборка его не декодирует — замер упёрся бы не в
            # задержку, а в отсутствие кодека. executablePath нужен там,
            # где браузер в образе не совпадает по сборке с playwright.
            launch: dict = {"args": [
                "--autoplay-policy=no-user-gesture-required",
                "--disable-dev-shm-usage",
            ]}
            if args.chromium:
                launch["executable_path"] = args.chromium
            browser = p.chromium.launch(**launch)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{page_port}/index.html")
            # Ждём, пока плеер реально начнёт показывать кадры: до этого
            # переходов нет и мерить нечего.
            deadline = time.time() + 45
            while time.time() < deadline:
                err = page.evaluate("window.__err")
                if err:
                    raise SystemExit(f"плеер не запустился: {err}")
                if page.evaluate("v.currentTime > 0 && !v.paused"):
                    break
                time.sleep(0.25)
            else:
                raise SystemExit("плеер не начал воспроизведение за 45 с")

            # Собираем переходы, пока идёт поток.
            stop_at = time.time() + duration - args.warmup
            while time.time() < stop_at:
                if page.evaluate("window.__events.length") >= args.transitions + 2:
                    break
                time.sleep(0.5)
            events = page.evaluate("window.__events")
            browser.close()
    finally:
        pub.stop()
        if page_srv:
            page_srv.terminate()
        mtx.terminate()
        try:
            mtx.wait(timeout=5)
        except subprocess.TimeoutExpired:
            mtx.kill()

    if not events:
        raise SystemExit("браузер не увидел ни одного перехода яркости")

    # Сопоставление идёт по системным часам, а НЕ по mediaTime: шкала HLS
    # у MediaMTX своя, её ноль — момент запуска муксера, а не t=0 у
    # источника (замерено: расхождение ~2 с, из-за него первая версия
    # сопоставления попадала в переход другого цвета).
    #
    # Поэтому: для увиденного перехода цвета C ищем ПОСЛЕДНИЙ по
    # расписанию переход того же цвета, снятый не позже момента показа.
    # Однозначно это ровно тогда, когда задержка меньше полупериода
    # мигания — отсюда и выбор периода в 8 с при нормативе 3 с: любая
    # величина вплоть до 4 с разрешается без двусмысленности, а выход за
    # неё виден как отдельная ошибка, а не как тихо заниженный результат.
    schedule = pub.transitions()
    half = FLASH_PERIOD_SEC / 2
    rows, aliased = [], 0
    for ev in events:
        seen_at = ev["epoch"] / 1000.0
        same = [t for (t, c) in schedule if c == ev["state"]
                and pub.epoch_of_stream_time(t) <= seen_at]
        if not same:
            continue
        t_sched = max(same)
        lat = seen_at - pub.epoch_of_stream_time(t_sched)
        if lat >= half:
            # Задержка не меньше полупериода: сопоставление перестало быть
            # однозначным. Молча взять такой отсчёт значило бы напечатать
            # заниженное число — считаем его отдельно и говорим вслух.
            aliased += 1
            continue
        rows.append({"stream_t": t_sched, "state": ev["state"],
                     "latency_sec": round(lat, 3)})

    if not rows:
        raise SystemExit(
            "ни один переход не сопоставился с расписанием источника\n"
            f"  события браузера: {events[:6]}\n"
            f"  расписание источника: {schedule[:6]}\n"
            f"  привязка часов: t=0 в {pub.epoch_of_stream_time(0):.3f}, "
            f"сейчас {time.time():.3f}")

    lat = sorted(r["latency_sec"] for r in rows)
    return {
        "normative_sec": NORMATIVE_SEC,
        "transport": "LL-HLS (hls.js)",
        "hls": hls_cfg,
        "fps": args.fps,
        "sync_count": args.sync_count,
        "samples": len(lat),
        "progress_samples": pub.samples,
        "aliased_samples": aliased,
        "min_sec": round(lat[0], 3),
        "median_sec": round(statistics.median(lat), 3),
        "max_sec": round(lat[-1], 3),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--transitions", type=int, default=8,
                    help="сколько переходов яркости зачесть (по умолчанию 8)")
    ap.add_argument("--fps", type=int, default=15,
                    help="частота кадров источника (SPEC §1: 15-30)")
    ap.add_argument("--warmup", type=float, default=6.0,
                    help="секунд на разгон плеера до начала счёта")
    ap.add_argument("--sync-count", type=int, default=1,
                    help="liveSyncDurationCount у hls.js — глубина буфера "
                         "живого края (1 = профиль фронтенда). Больше "
                         "единицы нужно для контрольного прогона: замер "
                         "обязан РЕАГИРОВАТЬ на заведомое изменение буфера, "
                         "иначе он мерит константу, а не задержку")
    ap.add_argument("--mediamtx", help="путь к бинарнику MediaMTX")
    ap.add_argument("--chromium", default=os.environ.get("BENCH_CHROMIUM"),
                    help="путь к бинарнику Chromium (или $BENCH_CHROMIUM), "
                         "если сборка в образе не совпадает с playwright")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    res = measure(args)

    # Норматив проверяется по ХУДШЕМУ прогону, как и §19 в цикле 44:
    # оператор видит не медиану, а тот кадр, который перед ним сейчас.
    worst = res["max_sec"]
    res["verdict"] = "ok" if worst <= NORMATIVE_SEC else "violated"

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        # Код возврата и в JSON-режиме: именно так замер вызывается из CI,
        # и молчаливый ноль на нарушении норматива сделал бы шаг
        # декоративным.
        return 0 if worst <= NORMATIVE_SEC else 1

    print(f"Задержка live, вся цепочка до картинки в браузере "
          f"({res['transport']}, {res['fps']} fps)")
    print(f"  сегмент {res['hls']['hlsSegmentDuration']}, "
          f"часть {res['hls']['hlsPartDuration']}, "
          f"окно {res['hls']['hlsSegmentCount']}")
    print(f"  замеров: {res['samples']} переходов "
          f"({res['progress_samples']} отсчётов привязки часов)")
    print(f"  минимум:  {res['min_sec']:.3f} с")
    print(f"  медиана:  {res['median_sec']:.3f} с")
    print(f"  максимум: {res['max_sec']:.3f} с")
    if res["aliased_samples"]:
        print(f"  отброшено как неоднозначные: {res['aliased_samples']} "
              f"(задержка ≥ полупериода {FLASH_PERIOD_SEC / 2:.0f} с — "
              f"поднимите --transitions и период мигания)")
    verdict = "выполнен" if worst <= NORMATIVE_SEC else "НЕ ВЫПОЛНЕН"
    print(f"\n  норматив ≤ {NORMATIVE_SEC:.0f} с по худшему: {verdict} "
          f"({worst:.3f} с, запас {NORMATIVE_SEC / worst:.2f}×)")
    return 0 if worst <= NORMATIVE_SEC else 1


if __name__ == "__main__":
    sys.exit(main())
