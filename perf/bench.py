#!/usr/bin/env python3
"""Бенчмарки горячих путей FaceWatch (SPEC §19, §23, §26).

Меряется то, что определяет, влезет ли система в бюджет целевого сервера
(2× Xeon E5-2670, 64 потока, без GPU, без AVX2 — SPEC §23):

* **decode** — кадров в секунду при декодировании основного потока (720p
  H.265). Определяет потолок слоя аналитики: SPEC §26 требует ≥ 5 FPS на
  канал, §23 отводит аналитике 2–3 ядра.
* **prefilter** — стоимость префильтра движения (MOG2/KNN, SPEC §19).
  Идёт по каждому кадру до детектора, поэтому его цена входит в бюджет
  целиком.
* **inference** — миллисекунд на лицо на CPU (SPEC §23: «бенчмарк
  инференса на целевом CPU в CI»).

Запуск:

    python perf/bench.py                # всё, человекочитаемо
    python perf/bench.py --json         # машиночитаемо, для CI
    python perf/bench.py --only decode  # одна группа

Осознанные ограничения, которые нельзя лечить в песочнице:

* **Числа не переносятся на целевой сервер напрямую.** У E5-2670 нет AVX2
  и слабее single-thread; любой современный хост даёт завышенный
  результат. Смысл прогона в CI — не абсолют, а **отслеживание
  деградации между циклами** на одинаковом железе раннера.
* Кодек берётся из SPEC §1 (H.265 720p 15 fps 2048 kbps), но клип
  синтетический: реальный поток с камеры содержит больше межкадровой
  избыточности, и настоящий decode будет быстрее. Занижение безопаснее
  завышения.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

# SPEC §1: «Основной поток: H.265, 1280×720 @ 15 fps, 2048 kbps».
CLIP_W, CLIP_H, CLIP_FPS, CLIP_KBPS = 1280, 720, 15, 2048
CLIP_SECONDS = 20

# SPEC §6/§19: детекция идёт по кадру, ужатому до detect_width (640 по
# умолчанию) — инференс меряется на этом размере, а не на исходном.
DETECT_W, DETECT_H = 640, 384


def _have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def make_clip(path: str, seconds: int = CLIP_SECONDS) -> dict:
    """Синтетический H.265-клип с параметрами основного потока из SPEC §1.

    `testsrc2` вместо статичной картинки намеренно: на неподвижном
    изображении H.265 сжимает почти в ничто, декодер простаивает, и
    измеренный FPS не имеет отношения к реальному потоку с камеры.
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={CLIP_W}x{CLIP_H}:rate={CLIP_FPS}",
        "-t", str(seconds),
        "-c:v", "libx265", "-b:v", f"{CLIP_KBPS}k",
        "-x265-params", "log-level=none",
        "-pix_fmt", "yuv420p", path,
    ]
    t0 = time.perf_counter()
    subprocess.run(cmd, check=True, capture_output=True)
    return {
        "encode_sec": round(time.perf_counter() - t0, 2),
        "size_bytes": os.path.getsize(path),
        "frames": seconds * CLIP_FPS,
    }


# --- decode ---------------------------------------------------------------

def bench_decode_opencv(path: str) -> dict:
    """`cv2.VideoCapture` — то, чем воркер декодирует сейчас."""
    import cv2

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return {"error": "VideoCapture не открыл клип"}
    frames = 0
    t0 = time.perf_counter()
    while True:
        ok, _ = cap.read()
        if not ok:
            break
        frames += 1
    elapsed = time.perf_counter() - t0
    cap.release()
    return {"frames": frames, "seconds": round(elapsed, 3),
            "fps": round(frames / elapsed, 1) if elapsed else 0}


def bench_decode_ffmpeg_pipe(path: str) -> dict:
    """FFmpeg → сырой BGR в пайп: альтернатива, которую SPEC §28 называет
    равноправной («OpenCV / FFmpeg (CPU-декод), GStreamer опционально»).

    Меряется честно: кадры читаются из пайпа в Python, как это делал бы
    воркер, а не отбрасываются в `-f null`, — иначе сравнение было бы с
    декодером без доставки кадров потребителю.
    """
    frame_bytes = CLIP_W * CLIP_H * 3
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames = 0
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        frames += 1
    proc.stdout.close()
    proc.wait()
    elapsed = time.perf_counter() - t0
    return {"frames": frames, "seconds": round(elapsed, 3),
            "fps": round(frames / elapsed, 1) if elapsed else 0}


def bench_decode_ffmpeg_scaled(path: str) -> dict:
    """FFmpeg с ресайзом до detect_width средствами самого декодера.

    Проверяет гипотезу, которая для слоя аналитики важнее абсолютного
    FPS: масштабирование внутри FFmpeg (SIMD, C) против `cv2.resize`
    после доставки полного кадра в Python. На 720p → 640 разница в
    объёме передаваемых через пайп данных четырёхкратная.
    """
    frame_bytes = DETECT_W * DETECT_H * 3
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", path,
           "-vf", f"scale={DETECT_W}:{DETECT_H}",
           "-f", "rawvideo", "-pix_fmt", "bgr24", "-"]
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    frames = 0
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        frames += 1
    proc.stdout.close()
    proc.wait()
    elapsed = time.perf_counter() - t0
    return {"frames": frames, "seconds": round(elapsed, 3),
            "fps": round(frames / elapsed, 1) if elapsed else 0}


# --- префильтр движения ---------------------------------------------------

def bench_motion_prefilter(path: str) -> dict:
    """MOG2 против KNN (SPEC §19: «Префильтр движения (MOG2/KNN)»).

    Префильтр идёт по каждому кадру до детектора, поэтому его цена
    входит в бюджет аналитики целиком, а не «иногда».
    """
    import cv2

    out = {}
    for name, ctor in (("MOG2", cv2.createBackgroundSubtractorMOG2),
                       ("KNN", cv2.createBackgroundSubtractorKNN)):
        cap = cv2.VideoCapture(path)
        sub = ctor()
        times = []
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            small = cv2.resize(frame, (DETECT_W, DETECT_H))
            t0 = time.perf_counter()
            mask = sub.apply(small)
            cv2.countNonZero(mask)
            times.append((time.perf_counter() - t0) * 1000)
        cap.release()
        if times:
            out[name] = {
                "ms_per_frame": round(statistics.mean(times), 3),
                "fps_ceiling": round(1000.0 / statistics.mean(times), 1),
                "frames": len(times),
            }
    return out


# --- инференс -------------------------------------------------------------

def bench_inference() -> dict:
    """Миллисекунд на лицо на CPU (SPEC §23).

    Модель детекции проекта (`buffalo_s`/SCRFD из insightface) весит
    сотни мегабайт и тянется из сети, которой в CI может не быть.
    Поэтому меряется **синтетическая свёрточная сеть сопоставимой формы**
    на ONNX Runtime CPU: цель бенчмарка — ловить деградацию рантайма и
    сборки onnxruntime между циклами на одинаковом железе, а не выдать
    абсолют для конкретной модели.

    Абсолютные ms/лицо на целевом железе (E5-2670, без AVX2) этим
    измерением НЕ заменяются — см. known gaps отчёта цикла.
    """
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError as exc:
        return {"skipped": f"нет зависимости: {exc.name}"}

    model = _synthetic_onnx_model()
    if model is None:
        return {"skipped": "не удалось собрать синтетическую модель"}

    so = ort.SessionOptions()
    # Один поток: на целевом сервере каждая analytics-камера идёт своим
    # процессом (§23, NUMA-привязка), и межпоточный параллелизм внутри
    # сессии там только мешает соседям.
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    sess = ort.InferenceSession(model, so, providers=["CPUExecutionProvider"])
    name = sess.get_inputs()[0].name
    batch = np.random.rand(1, 3, 112, 112).astype(np.float32)

    for _ in range(5):                       # прогрев
        sess.run(None, {name: batch})
    times = []
    for _ in range(50):
        t0 = time.perf_counter()
        sess.run(None, {name: batch})
        times.append((time.perf_counter() - t0) * 1000)

    return {
        "providers": ort.get_available_providers(),
        "ms_per_face_mean": round(statistics.mean(times), 2),
        "ms_per_face_p95": round(sorted(times)[int(len(times) * 0.95)], 2),
        "faces_per_sec": round(1000.0 / statistics.mean(times), 1),
        "note": "синтетическая сеть, не buffalo_s — см. докстринг",
    }


def _synthetic_onnx_model() -> bytes | None:
    """Свёрточная сеть формы, близкой к MobileFaceNet (вход 112×112×3).

    Собирается через onnx.helper, если пакет есть; иначе — None, и
    бенчмарк инференса честно помечается пропущенным.
    """
    try:
        import numpy as np
        import onnx
        from onnx import TensorProto, helper, numpy_helper
    except ImportError:
        return None

    nodes, inits = [], []
    ch_in, size = 3, 112
    for i, ch_out in enumerate((32, 64, 128, 256)):
        w = numpy_helper.from_array(
            np.random.rand(ch_out, ch_in, 3, 3).astype(np.float32), f"w{i}")
        inits.append(w)
        nodes.append(helper.make_node(
            "Conv", [f"x{i}", f"w{i}"], [f"c{i}"],
            kernel_shape=[3, 3], strides=[2, 2], pads=[1, 1, 1, 1]))
        nodes.append(helper.make_node("Relu", [f"c{i}"], [f"x{i + 1}"]))
        ch_in, size = ch_out, size // 2
    nodes.append(helper.make_node("GlobalAveragePool", ["x4"], ["y"]))

    graph = helper.make_graph(
        nodes, "synthetic_face_net",
        [helper.make_tensor_value_info("x0", TensorProto.FLOAT, [1, 3, 112, 112])],
        [helper.make_tensor_value_info("y", TensorProto.FLOAT, [1, 256, 1, 1])],
        inits)
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 10
    try:
        onnx.checker.check_model(model)
    except Exception:
        return None
    return model.SerializeToString()


# --- runner ---------------------------------------------------------------

def run(groups: set[str]) -> dict:
    result: dict = {
        "cpu": _cpu_name(),
        "cores": os.cpu_count(),
        "avx2": _has_avx2(),
        "clip": {"width": CLIP_W, "height": CLIP_H, "fps": CLIP_FPS,
                 "kbps": CLIP_KBPS, "seconds": CLIP_SECONDS},
    }

    needs_clip = groups & {"decode", "prefilter"}
    tmpdir = tempfile.mkdtemp(prefix="facewatch-bench-")
    try:
        clip = os.path.join(tmpdir, "main.mp4")
        if needs_clip:
            if not _have("ffmpeg"):
                result["error"] = "ffmpeg не найден — decode/prefilter пропущены"
                needs_clip = set()
            else:
                result["clip"].update(make_clip(clip))

        if "decode" in groups and needs_clip:
            result["decode"] = {
                "opencv_videocapture": bench_decode_opencv(clip),
                "ffmpeg_pipe": bench_decode_ffmpeg_pipe(clip),
                "ffmpeg_pipe_scaled_640": bench_decode_ffmpeg_scaled(clip),
            }
        if "prefilter" in groups and needs_clip:
            result["prefilter"] = bench_motion_prefilter(clip)
        if "inference" in groups:
            result["inference"] = bench_inference()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return result


def _cpu_name() -> str:
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "неизвестно"


def _has_avx2() -> bool:
    """Целевой сервер (E5-2670, Sandy Bridge) AVX2 не имеет — SPEC §23.

    Флаг попадает в отчёт, чтобы числа с раннера, у которого AVX2 есть,
    нельзя было случайно принять за числа целевого железа.
    """
    try:
        with open("/proc/cpuinfo") as fh:
            return " avx2 " in " " + fh.read().replace("\n", " ") + " "
    except OSError:
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", action="store_true", help="машиночитаемый вывод")
    ap.add_argument("--only", action="append", default=None,
                    choices=["decode", "prefilter", "inference"],
                    help="выполнить только указанные группы (можно повторять)")
    args = ap.parse_args()

    groups = set(args.only) if args.only else {"decode", "prefilter", "inference"}
    res = run(groups)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    print(f"CPU: {res['cpu']}  ядер: {res['cores']}  AVX2: "
          f"{'есть' if res['avx2'] else 'НЕТ (как на целевом сервере)'}")
    if not res["avx2"]:
        print("  (совпадает с целевым сервером по AVX2)")
    else:
        print("  ВНИМАНИЕ: у целевого сервера (E5-2670) AVX2 нет — числа завышены")

    if "decode" in res:
        print("\nДекодирование основного потока (720p H.265):")
        for name, d in res["decode"].items():
            if "error" in d:
                print(f"  {name:28} ОШИБКА: {d['error']}")
            else:
                print(f"  {name:28} {d['fps']:8.1f} FPS  ({d['frames']} кадров "
                      f"за {d['seconds']} с)")
    if "prefilter" in res:
        print("\nПрефильтр движения (кадр 640×384):")
        for name, d in res["prefilter"].items():
            print(f"  {name:28} {d['ms_per_frame']:8.3f} мс/кадр  "
                  f"(потолок {d['fps_ceiling']} FPS)")
    if "inference" in res:
        inf = res["inference"]
        print("\nИнференс на CPU:")
        if "skipped" in inf:
            print(f"  пропущено: {inf['skipped']}")
        else:
            print(f"  {'ms/лицо (среднее)':28} {inf['ms_per_face_mean']:8.2f}")
            print(f"  {'ms/лицо (p95)':28} {inf['ms_per_face_p95']:8.2f}")
            print(f"  {'лиц/с':28} {inf['faces_per_sec']:8.1f}")
            print(f"  провайдеры: {', '.join(inf['providers'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
