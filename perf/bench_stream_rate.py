#!/usr/bin/env python3
"""Цена FPS и битрейта §9 на проходе менеджера.

Обе величины считаются в `publish_record_layer_status()` — в том же цикле,
который синхронизирует пути записи и ставит статусы камер. SPEC §2 требует
независимости слоёв, а §19 — «CPU ≤ 0.04 ядра/камера» на записи, поэтому
украшение строки мониторинга обязано стоить около нуля. Замер отвечает на
три вопроса:

1. сколько стоит арифметика битрейта на 120 камерах (чистый Python);
2. сколько стоит одна проба ffprobe на сегменте боевого размера;
3. сколько добавляет к проходу менеджера бюджет проб `FPS_PROBE_BUDGET`.

Третье число — то, ради которого бюджет и введён: без него объект, у
которого сегменты перевернулись одновременно (общий рестарт MediaMTX),
получил бы 120 проб в одном проходе.

Сегмент для пробы генерируется ffmpeg'ом: замеряется стоимость ЧТЕНИЯ
индекса, и она зависит от числа пакетов и размера файла, а не от того,
кто файл записал. Правдивость самой величины на файлах MediaMTX проверяет
`worker/tests/test_stream_rate_live.py`, здесь — только цена.

Запуск: python perf/bench_stream_rate.py [--cameras 120] [--json]
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from stream_rate import (  # noqa: E402
    FPS_PROBE_BUDGET, SegmentFpsCache, probe_segment_fps, update_bitrates)

# Длительность и параметры кадра боевого сегмента (SPEC §20: 5-10 минут;
# §1: «720p-2K, 15-30 fps»). Берётся нижняя граница длительности и
# типичные 720p/25 — на них и планируется объект.
SEGMENT_SEC = 300
SEGMENT_FPS = 25
SEGMENT_SIZE = "1280x720"

REPEATS = 5


def _states(cameras: int, base_bytes: int = 0) -> dict[int, dict]:
    return {
        cam: {"camera_id": cam, "name": f"cam{cam}", "status": "online",
              "inbound_bytes": base_bytes + cam * 1000,
              "frames_in_error": 0, "online_since": None}
        for cam in range(1, cameras + 1)
    }


def bench_bitrate(cameras: int) -> dict:
    """Арифметика битрейта на всех камерах объекта."""
    samples: dict[int, tuple[float, int]] = {}
    _, samples = update_bitrates(samples, _states(cameras), 1000.0)

    times = []
    for i in range(REPEATS * 20):
        now = 1000.0 + (i + 1) * 10
        start = time.perf_counter()
        rates, samples = update_bitrates(samples, _states(cameras, 1_000_000 * (i + 1)), now)
        times.append(time.perf_counter() - start)
    assert all(v is not None for v in rates.values()), "битрейт не посчитался"
    return {"cameras": cameras,
            "median_ms": round(statistics.median(times) * 1000, 3),
            "max_ms": round(max(times) * 1000, 3)}


def _make_segment(path: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i",
         f"testsrc=size={SEGMENT_SIZE}:rate={SEGMENT_FPS}:duration={SEGMENT_SEC}",
         "-c:v", "libx264", "-preset", "ultrafast", "-g", str(SEGMENT_FPS),
         "-movflags", "frag_keyframe+empty_moov+default_base_moof",
         "-f", "mp4", str(path), "-y"],
        check=True, capture_output=True)


def bench_probe(path: Path) -> dict:
    """Одна проба ffprobe на сегменте боевого размера."""
    times = []
    fps = None
    for _ in range(REPEATS):
        start = time.perf_counter()
        fps = probe_segment_fps(str(path))
        times.append(time.perf_counter() - start)
    if fps is None:
        raise SystemExit("ffprobe не отдал FPS — замер бессмыслен")
    if abs(fps - SEGMENT_FPS) > 1.0:
        raise SystemExit(f"проба врёт: {fps} вместо {SEGMENT_FPS}")
    return {"segment_sec": SEGMENT_SEC, "fps_measured": fps,
            "size_mb": round(path.stat().st_size / 1048576, 1),
            "median_ms": round(statistics.median(times) * 1000, 1),
            "max_ms": round(max(times) * 1000, 1)}


def bench_tick(path: Path, cameras: int) -> dict:
    """Худший проход: у всех камер сегмент перевернулся одновременно.

    Считается и то, что было бы без бюджета, — ради этого числа бюджет и
    введён.
    """
    cache = SegmentFpsCache(budget=FPS_PROBE_BUDGET)
    newest = {cam: (1000.0, str(path)) for cam in range(1, cameras + 1)}

    start = time.perf_counter()
    cache.refresh(newest, 1000.0, 10_000)
    budgeted = time.perf_counter() - start

    # Проба одного файла — та же цена, что и любого другого; стоимость
    # непробюджетированного прохода считается как N × одна проба, а не
    # прогоняется 120 раз: это те же 8 секунд, потраченные впустую.
    one = statistics.median(
        [_timed(lambda: probe_segment_fps(str(path))) for _ in range(REPEATS)])

    # Установившийся режим наступает не со второго прохода: первый пробует
    # только `budget` камер из `cameras`. Обход надо ДОГНАТЬ до конца —
    # первая редакция замера этого не делала и печатала «установившийся
    # проход 500 мс», то есть измеряла всё те же пробы, только под другим
    # заголовком.
    ticks_to_steady = 1
    while True:
        start = time.perf_counter()
        cache.refresh(newest, 1000.0, 10_000)
        cached = time.perf_counter() - start
        if cached * 1000 < 1.0:      # ffprobe не звался ни разу
            break
        ticks_to_steady += 1
        if ticks_to_steady > cameras:
            raise SystemExit("обход не сходится: камеры пробуются бесконечно")

    return {"cameras": cameras, "budget": FPS_PROBE_BUDGET,
            "worst_tick_ms": round(budgeted * 1000, 1),
            "without_budget_ms": round(one * cameras * 1000, 1),
            "ticks_to_steady": ticks_to_steady,
            "steady_tick_ms": round(cached * 1000, 3)}


def _timed(fn) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", type=int, default=120)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        print("нужны ffmpeg и ffprobe", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        segment = Path(tmp) / "segment.mp4"
        _make_segment(segment)
        result = {
            "cpu": platform.processor() or platform.machine(),
            "bitrate": bench_bitrate(args.cameras),
            "probe": bench_probe(segment),
            "tick": bench_tick(segment, args.cameras),
        }

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    b, p, t = result["bitrate"], result["probe"], result["tick"]
    print(f"CPU: {result['cpu']}")
    print(f"битрейт {b['cameras']} камер: медиана {b['median_ms']} мс, "
          f"максимум {b['max_ms']} мс")
    print(f"проба ffprobe ({p['segment_sec']} с, {p['size_mb']} МБ): "
          f"медиана {p['median_ms']} мс, измерено {p['fps_measured']} к/с")
    print(f"худший проход ({t['cameras']} камер, бюджет {t['budget']}): "
          f"{t['worst_tick_ms']} мс "
          f"(без бюджета было бы {t['without_budget_ms']} мс)")
    print(f"установившийся проход (все сегменты пробованы, достигнут за "
          f"{t['ticks_to_steady']} прохода/ов): {t['steady_tick_ms']} мс")

    # Проход менеджера идёт раз в 5 секунд (worker.manager). Бюджет обязан
    # держать добавку заметно ниже этого, иначе синхронизация путей записи
    # начнёт отставать от собственного цикла.
    if t["worst_tick_ms"] > 1500:
        print("РЕГРЕСС: худший проход дороже 1.5 с — бюджет проб не держит",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
