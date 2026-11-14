#!/usr/bin/env python3
"""§19 «0 пропущенных сегментов при штатной работе» — первый замер.

Из нормативов §19 этот оставался неизмеренным дольше всех, и не случайно:
остальные меряются одним процессом, а этот требует, чтобы **настоящий**
MediaMTX несколько раз перевернул сегмент на настоящем RTSP-потоке. Ни
двойник Control API, ни юнит-тест ротации ответить на него не могут: они
проверяют, что мы правильно просим, а норматив — про то, сколько видео
теряется в момент, когда сервер закрывает один файл и открывает следующий.

**Что именно меряется.** Не «сколько файлов появилось» — это считает и
существующий `test_segments_land_in_the_configured_media_root`. Считаются
секунды: имя файла даёт unix-начало сегмента (`cam{id}_{epoch}.mp4`,
`record_layer.parse_segment_name`), ffprobe — его фактическую длительность.
Разрыв на стыке i и i+1:

    gap_i = start_{i+1} − (start_i + duration_i)

`gap > 0` — потерянное видео, то есть нарушение норматива. `gap < 0` —
перекрытие, для непрерывности архива безвредно. Итог — покрытие: сумма
длительностей против всего времени наблюдения, от начала первого сегмента
до конца последнего.

**Разрешающая способность стыка — одна секунда, и это не оговорка, а
свойство схемы имён.** `recordPath` кончается на `%s`, то есть unix-время
в целых секундах (`record_layer.record_path_template`), поэтому начало
сегмента известно с точностью до секунды и разрыв меньше секунды по
стыкам не виден в принципе. Отсюда и бюджет норматива: меньше секунды —
меньше одного GOP при 15 fps и `-g 15`, то есть «пропуска сегмента» такой
величины не бывает. Второе число, `recorded` против `observed`, считается
мимо имён — по фактическим длительностям против настенных часов прогона —
и субсекундную недостачу как раз показывает, но в неё входят и края
(путь заведён → пошёл первый кадр, и закрытие последнего файла), а не
только стыки.

**Отклонение от боевой конфигурации ровно одно, и оно консервативно.**
Путь заводится production-функцией `record_layer.path_conf()` целиком, но
с минутным сегментом вместо пятиминутного (§20): за три минуты прогона это
даёт три переворота вместо половины одного. Норматив нарушается **на
стыке**, поэтому чаще переворачивать — значит дать ему больше шансов
проявиться, а не меньше.

Чего замер не заменяет: на объекте камеры отдают H.265 по сети с потерями,
а не синтетический H.264 по локальной петле. Число разрывов на настоящем
парке остаётся пунктом чек-листа развёртывания (раздел 1).

Запуск (нужны `MEDIAMTX_BIN` и ffmpeg/ffprobe):

    MEDIAMTX_BIN=/tmp/mtx/mediamtx python perf/bench_segment_continuity.py
    python perf/bench_segment_continuity.py --minutes 5 --json
"""
import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "worker"))

from record_layer import (MediaMTXClient, parse_segment_name,  # noqa: E402
                          path_conf, path_name, segments_dir, sync_paths)

# Норматив §19: пропущенных сегментов быть не должно, то есть потерянного
# видео — ноль. Ноль в чистом виде на измерении времени недостижим (кванты
# контейнера fMP4), поэтому норматив считается выполненным, если суммарная
# потеря не превышает длительности одного GOP: меньше этого «пропуска
# сегмента» не бывает, это граница разрешающей способности замера.
GOP_SEC = 1.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout: float, message: str, interval: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if pred():
                return
        except Exception as exc:
            last = exc
        time.sleep(interval)
    raise SystemExit(f"{message} (последняя ошибка: {last})")


def _probe_duration(path: str) -> float:
    """Фактическая длительность записанного сегмента, секунды.

    Именно ffprobe, а не разница имён соседних файлов: имя даёт заявленное
    начало, и если сегмент оборвался на середине, разница имён этого не
    покажет — а норматив нарушен ровно в этом случае.
    """
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def run(minutes: int, camera_id: int = 1) -> dict:
    mediamtx = os.environ.get("MEDIAMTX_BIN")
    if not mediamtx or not os.path.exists(mediamtx):
        raise SystemExit("нужен MEDIAMTX_BIN — путь к бинарнику MediaMTX "
                         "той же версии, что в docker-compose.yml")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            raise SystemExit(f"нужен {tool}")

    workdir = tempfile.mkdtemp(prefix="fw-continuity-")
    media_root = os.path.join(workdir, "media")
    seg_dir = segments_dir(media_root)
    os.makedirs(seg_dir, exist_ok=True)
    api_port, rtsp_port = _free_port(), _free_port()
    conf = Path(workdir) / "mediamtx.yml"
    conf.write_text(
        "logLevel: warn\n"
        "api: yes\n"
        f"apiAddress: 127.0.0.1:{api_port}\n"
        f"rtspAddress: :{rtsp_port}\n"
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        "pathDefaults:\n"
        "  record: no\n"
        "  recordPartDuration: 1s\n"
        "paths:\n"
        "  fakecam:\n"
        "    source: publisher\n",
        encoding="utf-8")

    mtx = subprocess.Popen([mediamtx, str(conf)], cwd=workdir,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cam = None
    try:
        client = MediaMTXClient(f"http://127.0.0.1:{api_port}", timeout=5)
        _wait(lambda: client.list_path_configs() is not None, 20,
              "Control API не поднялся")

        # 720p 15 fps — профиль основного потока из §1 («H.265, 720p-2K,
        # 15-30 fps»). Кодек H.264: слой записи remux'ит поток как есть и
        # кодека не касается вовсе (`path_conf`: ни кодек, ни битрейт не
        # задаются), а libx265 в песочнице съел бы всё CPU и сам стал бы
        # источником разрывов — то есть замер мерил бы кодировщик.
        cam = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
             "-g", "15", "-pix_fmt", "yuv420p",
             "-f", "rtsp", "-rtsp_transport", "tcp",
             f"rtsp://127.0.0.1:{rtsp_port}/fakecam"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait(lambda: any(p.get("ready") for p in client.runtime_paths().values()),
              25, "источник не начал публиковаться")

        desired = {path_name(camera_id): path_conf(
            f"rtsp://127.0.0.1:{rtsp_port}/fakecam",
            segment_duration_min=1, media_root=media_root)}
        stats = sync_paths(client, desired)
        if stats["failed"]:
            raise SystemExit(f"путь не заведён: {stats}")

        started = time.time()
        time.sleep(minutes * 60)
        observed = time.time() - started

        # Путь снимается до замера: MediaMTX дописывает текущий сегмент при
        # закрытии, и без этого последний файл ffprobe увидел бы обрезанным
        # — замер показал бы «потерю», которой на объекте нет.
        sync_paths(client, {})
        time.sleep(3)
    finally:
        for proc in (cam, mtx):
            if proc is not None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()

    segments = []
    for name in sorted(os.listdir(seg_dir)):
        parsed = parse_segment_name(name)
        if not parsed or parsed[0] != camera_id:
            continue
        full = os.path.join(seg_dir, name)
        segments.append({"name": name, "start": parsed[1],
                         "duration": _probe_duration(full),
                         "bytes": os.path.getsize(full)})
    shutil.rmtree(workdir, ignore_errors=True)

    if len(segments) < 2:
        raise SystemExit(
            f"ротации не случилось: сегментов {len(segments)} за {minutes} мин — "
            "мерить непрерывность нечем")

    gaps = []
    for prev, nxt in zip(segments, segments[1:]):
        gaps.append(round(nxt["start"] - (prev["start"] + prev["duration"]), 3))
    lost = round(sum(g for g in gaps if g > 0), 3)
    recorded = round(sum(s["duration"] for s in segments), 3)
    span = round((segments[-1]["start"] + segments[-1]["duration"]) - segments[0]["start"], 3)

    return {
        "minutes": minutes,
        "observed_sec": round(observed, 1),
        "segments": len(segments),
        "rotations": len(segments) - 1,
        "recorded_sec": recorded,
        "span_sec": span,
        "coverage_pct": round(100.0 * recorded / span, 3) if span > 0 else 0.0,
        # Недостача против настенных часов — единственное число замера, не
        # ограниченное секундным разрешением имён. В неё входят края
        # прогона, поэтому она не сравнивается с бюджетом норматива, а
        # печатается: рост от цикла к циклу означал бы, что теряться стало
        # больше, даже если по стыкам по-прежнему нули.
        "edge_deficit_sec": round(observed - recorded, 3),
        "gaps_sec": gaps,
        "worst_gap_sec": max(gaps) if gaps else 0.0,
        "lost_sec": lost,
        "budget_sec": GOP_SEC,
        "spec_ok": lost <= GOP_SEC,
        "sizes_bytes": [s["bytes"] for s in segments],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--minutes", type=int, default=3,
                    help="сколько минут писать; при минутном сегменте это "
                         "число переворотов (по умолчанию 3)")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    result = run(args.minutes)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"Сегментов: {result['segments']} "
              f"(переворотов {result['rotations']}) за {result['observed_sec']} с")
        print(f"Записано: {result['recorded_sec']} с из {result['span_sec']} с "
              f"наблюдения — покрытие {result['coverage_pct']} %")
        print(f"Против настенных часов прогона: {result['recorded_sec']} с "
              f"из {result['observed_sec']} с, недостача "
              f"{result['edge_deficit_sec']} с (включая края, не только стыки)")
        print(f"Разрывы на стыках, с: {result['gaps_sec']} "
              f"(разрешение — 1 с, время в имени файла целое)")
        print(f"Потеряно суммарно: {result['lost_sec']} с "
              f"при бюджете {result['budget_sec']} с")
        print(f"§19 «0 пропущенных сегментов»: "
              f"{'ДА' if result['spec_ok'] else 'НЕТ'}")
    return 0 if result["spec_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
