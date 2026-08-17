#!/usr/bin/env python3
"""Бенчмарки горячих путей FaceWatch (SPEC §7, §15, §19).

Меряется то, что определяет, влезет ли система в бюджет сервера объекта.
Порядок нумерации — по редакции «Production NVR Edition» (2026-08-16):
§19 задаёт нормативы производительности, §7 — норматив поиска по архиву,
§15 — слой распознавания лиц, §20 — классы серверов (от N100 до 2× Xeon):

* **decode** — кадров в секунду при декодировании основного потока (720p
  H.265). Определяет потолок слоя аналитики: SPEC §19 требует ≥ 5 FPS на
  канал, §16 отводит камере analytics 0.5–1.5 ядра при 5 FPS.
* **prefilter** — стоимость префильтра движения (MOG2/KNN, SPEC §15:
  профили производительности аналитики).
  Идёт по каждому кадру до детектора, поэтому его цена входит в бюджет
  целиком.
* **inference** — миллисекунд на лицо на CPU (§21: ONNX Runtime CPU /
  OpenVINO как рекомендованный рантайм).
* **chain** — вся цепочка аналитики одним прогоном на РЕАЛЬНОЙ модели
  (`buffalo_s`): decode → префильтр → детектор → эмбеддинг, в том же
  порядке и с теми же параметрами, что в `worker.camera_worker()`. Это
  единственная группа, которая отвечает на вопрос §19 «детекция ≥ 5
  FPS/канал» целиком, а не по частям: сумма отдельно измеренных шагов не
  учитывает ни повторный ресайз, ни то, что детектор получает полный кадр.

Запуск:

    python perf/bench.py                # набор по умолчанию, человекочитаемо
    python perf/bench.py --json         # машиночитаемо, для CI
    python perf/bench.py --only decode  # одна группа
    python perf/bench.py --only chain   # полная цепочка (тянет модель из сети)

`chain`, `facesearch` и `archive` в набор по умолчанию не входят: первая
качает модель (~125 МБ), две другие засевают сотни тысяч строк в Postgres.

Осознанные ограничения, которые нельзя лечить в песочнице:

* **Числа не переносятся на сервер объекта напрямую.** У E5-2670 (верхний
  класс §20) нет AVX2
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


# --- полная цепочка аналитики (SPEC §15, §19) -----------------------------

# SPEC §19: «Детекция ≥ 5 FPS/канал на камерах analytics».
DETECTION_FPS_TARGET = 5.0


def _sample_face_image():
    """Кадр с настоящими лицами из состава insightface.

    Синтетический `testsrc2` для этой цепочки не годится: детектор не
    найдёт на нём ни одного лица, эмбеддинг не посчитается ни разу, и
    «полная цепочка» выродилась бы в decode + префильтр + детектор
    вхолостую — то есть в число заметно ЛУЧШЕ реального. Картинка идёт в
    составе пакета (Apache 2.0, §24), сеть для неё не нужна.
    """
    import os.path

    import cv2
    import insightface

    for name in ("t1.jpg", "Tom_Hanks_54745.png"):
        path = os.path.join(os.path.dirname(insightface.__file__), "data", "images", name)
        img = cv2.imread(path)
        if img is not None:
            return img, name
    return None, None


def make_face_clip(path: str, seconds: int = CLIP_SECONDS) -> dict:
    """H.265-клип параметров §1, в кадре которого есть реальные лица.

    Лица медленно едут по кадру: неподвижная картинка не даёт сработать
    префильтру движения, и MOG2 отсекал бы каждый кадр — детектор не
    запускался бы вовсе, а именно он самый дорогой шаг цепочки.
    """
    import cv2
    import numpy as np

    face_img, source = _sample_face_image()
    if face_img is None:
        return {"error": "в составе insightface нет образца с лицами"}

    # Лица занимают примерно половину высоты кадра — типичный план
    # проходной, где распознавание вообще имеет смысл.
    scale = (CLIP_H * 0.5) / face_img.shape[0]
    face = cv2.resize(face_img, (int(face_img.shape[1] * scale), int(CLIP_H * 0.5)))
    fh, fw = face.shape[:2]

    frames = seconds * CLIP_FPS
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "bgr24",
        "-s", f"{CLIP_W}x{CLIP_H}", "-r", str(CLIP_FPS), "-i", "-",
        "-c:v", "libx265", "-b:v", f"{CLIP_KBPS}k",
        "-x265-params", "log-level=none", "-pix_fmt", "yuv420p", path,
    ]
    rnd = np.random.RandomState(20260817)
    t0 = time.perf_counter()
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    try:
        for i in range(frames):
            # Слабый шум на фоне: сплошная заливка сжимается почти в ничто,
            # и декодер простаивал бы (та же оговорка, что у make_clip).
            frame = rnd.randint(0, 40, (CLIP_H, CLIP_W, 3), dtype=np.uint8)
            x = int((CLIP_W - fw) * (0.5 + 0.45 * np.sin(i / 12.0)))
            y = int((CLIP_H - fh) * (0.5 + 0.35 * np.cos(i / 17.0)))
            frame[y:y + fh, x:x + fw] = face
            proc.stdin.write(frame.tobytes())
        proc.stdin.close()
    except BrokenPipeError:
        pass
    rc = proc.wait()
    if rc != 0 or not os.path.exists(path):
        return {"error": f"ffmpeg вернул {rc}: {proc.stderr.read().decode()[:200]}"}
    return {
        "encode_sec": round(time.perf_counter() - t0, 2),
        "size_bytes": os.path.getsize(path),
        "frames": frames,
        "face_source": source,
    }


def bench_analytics_chain(path: str, single_thread: bool = True) -> dict:
    """Полная цепочка аналитики одним прогоном (SPEC §19: ≥ 5 FPS/канал).

    Это тот самый замер, который прошлые циклы держали в known gaps как
    «нужна реальная ONNX-модель»: `buffalo_s` тянется из сети, и вместо
    неё мерилась синтетическая сеть по частям. Здесь цепочка идёт целиком
    и **в том же порядке, что в worker.camera_worker()**:

        cap.read() → cv2.resize(frame, (640,360)) → MOG2.apply()
                   → FACE_APP.get(frame)  # детектор + эмбеддинг

    Важные детали соответствия production'у:

    * детектор получает ПОЛНЫЙ кадр (720p), а не уменьшенный: ужимает его
      сам insightface до `det_size`, и мерить на заранее уменьшенном
      кадре значило бы выбросить эту работу из бюджета;
    * `det_size` берётся из `detect_width` (640) — как в load_face_app();
    * `.get()` считает и эмбеддинг, поэтому отдельного шага для него нет.

    `single_thread=True` — честное число НА КАНАЛ: §16 отводит аналитике
    0.5–1.5 ядра на камеру, то есть камеры идут своими процессами и делят
    ядра, а не забирают все 64 каждая. Прогон в несколько потоков ORT
    показал бы FPS одной камеры на пустом сервере — величину, которой на
    объекте не существует.
    """
    if single_thread:
        # Должно быть выставлено ДО импорта onnxruntime: число потоков
        # OpenMP читается при загрузке рантайма, а не при создании сессии.
        for var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
            os.environ[var] = "1"
    try:
        import cv2
        import numpy as np
        from insightface.app import FaceAnalysis
    except ImportError as exc:
        return {"skipped": f"нет зависимости: {exc.name}"}

    if single_thread:
        cv2.setNumThreads(1)

    try:
        # Тот же набор модулей, что в worker.load_face_app(): бенчмарк
        # должен мерить production-путь, а не FaceAnalysis по умолчанию —
        # разница между ними шестикратная (см. worker.FACE_MODULES).
        app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"],
                           allowed_modules=["detection", "recognition"])
        app.prepare(ctx_id=0, det_size=(DETECT_W, DETECT_W))
    except Exception as exc:
        # Модель качается с GitHub при первом вызове. На раннере без сети
        # это не повод ронять весь бенчмарк — остальные группы полезны и
        # без неё, а пропуск виден в отчёте.
        return {"skipped": f"модель buffalo_s недоступна: {type(exc).__name__}: {exc}"}

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        return {"error": "VideoCapture не открыл клип"}
    bg = cv2.createBackgroundSubtractorMOG2()

    t_decode, t_prefilter, t_detect = [], [], []
    frames = faces_total = detector_runs = 0
    t_start = time.perf_counter()
    while True:
        t0 = time.perf_counter()
        ok, frame = cap.read()
        t1 = time.perf_counter()
        if not ok:
            break
        frames += 1

        small = cv2.resize(frame, (640, 360))
        fg = bg.apply(small)
        motion = int(np.count_nonzero(fg)) > 1500   # CONFIG["motion_threshold"]
        t2 = time.perf_counter()

        n_faces = 0
        if motion:
            detector_runs += 1
            n_faces = len(app.get(frame))
            faces_total += n_faces
        t3 = time.perf_counter()

        t_decode.append((t1 - t0) * 1000)
        t_prefilter.append((t2 - t1) * 1000)
        if motion:
            t_detect.append((t3 - t2) * 1000)
    wall = time.perf_counter() - t_start
    cap.release()

    if not frames:
        return {"error": "клип не содержит кадров"}

    chain_fps = frames / wall
    return {
        "frames": frames,
        "wall_sec": round(wall, 2),
        "chain_fps": round(chain_fps, 2),
        "target_fps": DETECTION_FPS_TARGET,
        "meets_target": chain_fps >= DETECTION_FPS_TARGET,
        "detector_runs": detector_runs,
        "faces_detected": faces_total,
        # Средние по шагам: сумма меньше wall на накладные расходы цикла,
        # это ожидаемо и не сводится «в ноль» специально.
        "ms_decode": round(statistics.mean(t_decode), 2),
        "ms_prefilter": round(statistics.mean(t_prefilter), 2),
        "ms_detect_embed": round(statistics.mean(t_detect), 2) if t_detect else None,
        "ms_detect_embed_p95": (round(sorted(t_detect)[int(len(t_detect) * 0.95)], 2)
                                if t_detect else None),
        "single_thread": single_thread,
        "model": "buffalo_s",
        "det_size": DETECT_W,
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


# --- поиск похожих лиц (SPEC §12, §26) ------------------------------------

# SPEC §12: «время поиска ≤ 3–5 сек на базе до 100 000 лиц».
FACESEARCH_ROWS = 100_000
EMBED_DIM = 512  # models.FaceEvent.embedding — Vector(512)

# Отдельная схема, а не таблицы приложения: бенчмарк засевает сотню тысяч
# строк, и делать это в `public` означало бы либо снести архив разработчика,
# либо оставить после себя 100k мусорных событий. `search_path` позволяет
# гонять НЕИЗМЕНЁННЫЙ production-SQL из routers/search.py — если бы запрос
# пришлось переписывать под бенчмарк, мерился бы уже не он.
FACESEARCH_SCHEMA = "bench_facesearch"

# Запрос скопирован из backend/app/routers/search.py дословно (без
# опциональных фильтров по дате и статусу, которых в базовом сценарии
# поиска по фото нет). Копия, а не импорт: бенчмарк не должен тянуть
# зависимости бэкенда, но расхождение с оригиналом обесценивает замер —
# при правке search.py эту строку нужно обновить.
FACESEARCH_SQL = """
    SELECT fe.id, fe.person_id, fe.camera_id, fe.ts, fe.snapshot_path,
           p.name, p.status,
           1 - (fe.embedding <=> CAST(%(vec)s AS vector)) AS similarity,
           (SELECT vs.id FROM video_segments vs
              WHERE vs.camera_id = fe.camera_id
                AND fe.ts >= vs.started_at AND fe.ts <= vs.ended_at
              ORDER BY vs.started_at DESC LIMIT 1) AS segment_id
    FROM face_events fe
    LEFT JOIN persons p ON p.id = fe.person_id
    WHERE fe.embedding IS NOT NULL
      AND (1 - (fe.embedding <=> CAST(%(vec)s AS vector))) >= %(threshold)s
    ORDER BY similarity DESC LIMIT %(limit)s
"""

# Тот же смысл, но порядок задан прямо оператором расстояния, а не
# выражением-псевдонимом: планировщик pgvector берёт HNSW-индекс только для
# `ORDER BY <столбец> <=> <константа>`. Разница между двумя вариантами и
# есть ответ на вопрос, работает ли индекс в production-запросе.
FACESEARCH_SQL_INDEXED = """
    SELECT fe.id, fe.person_id, fe.camera_id, fe.ts, fe.snapshot_path,
           p.name, p.status,
           1 - (fe.embedding <=> CAST(%(vec)s AS vector)) AS similarity,
           (SELECT vs.id FROM video_segments vs
              WHERE vs.camera_id = fe.camera_id
                AND fe.ts >= vs.started_at AND fe.ts <= vs.ended_at
              ORDER BY vs.started_at DESC LIMIT 1) AS segment_id
    FROM face_events fe
    LEFT JOIN persons p ON p.id = fe.person_id
    WHERE fe.embedding IS NOT NULL
    ORDER BY fe.embedding <=> CAST(%(vec)s AS vector) LIMIT %(limit)s
"""


# Число «персон» в засеваемой базе: эмбеддинги кладутся кластерами вокруг
# центроидов, а не равномерно по сфере. Это не украшательство, а условие
# осмысленности замера: случайные векторы в 512 измерениях почти
# ортогональны друг другу (косинусная схожесть ≈ 0), порог эндпоинта 0.4 не
# проходит НИ ОДНА строка, и запрос меряется на пустом результате — то есть
# не делает той работы (сортировка, LEFT JOIN, подзапрос на сегмент),
# ради которой его и меряют. Первый прогон этого бенчмарка выдал ровно
# такую картину: 0 строк на 2000 эмбеддингов.
FACESEARCH_CLUSTERS = 500

# Разброс внутри кластера. Считается не на глаз: для единичного центроида
# и шума со стандартным отклонением s по каждой из dim координат
# косинусная схожесть ≈ 1/sqrt(1 + s²·dim). При dim=512 значение 0.021
# даёт ≈ 0.90 — столько же, сколько у двух снимков одного человека.
#
# Первая версия брала 0.35 «на глаз»: шум полностью забивал центроид
# (компоненты единичного вектора в 512 измерениях имеют масштаб
# 1/sqrt(512) ≈ 0.044), схожесть выходила 0.12, и порог 0.4 снова не
# проходила ни одна строка — та же вырожденность, что и на равномерных
# векторах, только менее заметная.
FACESEARCH_SPREAD = 0.021


def _random_unit_vector(rnd, dim: int = EMBED_DIM) -> list[float]:
    """Нормированный вектор — как эмбеддинг лица.

    Косинусное расстояние определено на направлении, поэтому
    ненормированные случайные векторы дали бы распределение схожести, не
    похожее на настоящее, и порог отсекал бы не то количество строк.
    """
    v = [rnd.gauss(0.0, 1.0) for _ in range(dim)]
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def _near(rnd, centroid: list[float], spread: float = FACESEARCH_SPREAD) -> list[float]:
    """Вектор рядом с центроидом — повторное появление того же человека."""
    v = [c + rnd.gauss(0.0, spread) for c in centroid]
    norm = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / norm for x in v]


def _seed_facesearch(conn, rows: int, log=None) -> dict:
    """Засеять схему бенчмарка `rows` эмбеддингами и построить HNSW-индекс."""
    import io
    import random

    rnd = random.Random(20260816)
    timings: dict = {}
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {FACESEARCH_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {FACESEARCH_SCHEMA}")
        cur.execute(f"SET search_path = {FACESEARCH_SCHEMA}, public")
        cur.execute("CREATE TABLE persons (id serial PRIMARY KEY, name text, status text)")
        cur.execute(
            "CREATE TABLE video_segments (id serial PRIMARY KEY, camera_id int, "
            "started_at timestamp, ended_at timestamp)"
        )
        cur.execute(
            "CREATE TABLE face_events (id serial PRIMARY KEY, camera_id int, "
            "person_id int, ts timestamp, snapshot_path text, "
            f"embedding vector({EMBED_DIM}))"
        )
        # Немного персон и сегментов — LEFT JOIN и коррелированный подзапрос
        # на сегмент входят в измеряемый запрос и стоят своего времени.
        cur.execute(
            "INSERT INTO persons (name, status) SELECT 'p' || g, "
            "CASE WHEN g % 3 = 0 THEN 'known' ELSE 'unknown' END "
            "FROM generate_series(1, 500) g"
        )
        cur.execute(
            "INSERT INTO video_segments (camera_id, started_at, ended_at) "
            "SELECT (g % 120) + 1, NOW() - (g || ' minutes')::interval, "
            "NOW() - ((g - 5) || ' minutes')::interval "
            "FROM generate_series(1, 2000) g"
        )

        # Центроиды «персон»: вокруг них и раскладываются эмбеддинги.
        centroids = [_random_unit_vector(rnd) for _ in range(FACESEARCH_CLUSTERS)]

        t0 = time.perf_counter()
        batch = 2000
        done = 0
        while done < rows:
            n = min(batch, rows - done)
            buf = io.StringIO()
            for i in range(n):
                vec = _near(rnd, centroids[(done + i) % FACESEARCH_CLUSTERS])
                # Четыре знака после запятой: на 512 измерениях полная
                # точность раздувает COPY-поток в разы, не меняя ни
                # расстояний, ни поведения индекса.
                lit = "[" + ",".join(f"{x:.4f}" for x in vec) + "]"
                cam = (done + i) % 120 + 1
                person = (done + i) % 500 + 1
                buf.write(f"{cam}\t{person}\t2026-08-01 00:00:00\t/media/s.jpg\t{lit}\n")
            buf.seek(0)
            cur.copy_from(buf, "face_events",
                          columns=("camera_id", "person_id", "ts", "snapshot_path", "embedding"))
            done += n
            if log and done % 20000 == 0:
                log(f"  засеяно {done}/{rows}")
        timings["seed_sec"] = round(time.perf_counter() - t0, 2)

        t0 = time.perf_counter()
        cur.execute("CREATE INDEX ON face_events USING hnsw (embedding vector_cosine_ops)")
        cur.execute("CREATE INDEX ON video_segments (camera_id, started_at)")
        timings["index_build_sec"] = round(time.perf_counter() - t0, 2)

        t0 = time.perf_counter()
        cur.execute("ANALYZE face_events")
        cur.execute("ANALYZE video_segments")
        cur.execute("ANALYZE persons")
        timings["analyze_sec"] = round(time.perf_counter() - t0, 2)

    # Запрос идёт «фотографией» человека, который в базе есть: оператор
    # ищет known-персону, а не случайный шум. Вектор берётся рядом с
    # центроидом, а не самим центроидом, — точное совпадение с засеянной
    # строкой было бы вырожденным случаем.
    timings["_query_vec"] = _near(rnd, centroids[0])
    return timings


def _time_query(cur, sql: str, params: dict, repeats: int = 5) -> dict:
    samples = []
    for _ in range(repeats):
        t0 = time.perf_counter()
        cur.execute(sql, params)
        got = cur.fetchall()
        samples.append(time.perf_counter() - t0)
    return {
        "ms_mean": round(statistics.mean(samples) * 1000, 1),
        "ms_p95": round(max(samples) * 1000, 1),
        "rows_returned": len(got),
    }


def bench_facesearch(rows: int = FACESEARCH_ROWS, keep: bool = False, log=None) -> dict:
    """Поиск похожих лиц на базе из `rows` эмбеддингов (SPEC §12: ≤ 3–5 с).

    Меряется **запрос**, а не весь эндпоинт: извлечение эмбеддинга из
    загруженного фото идёт в воркере (это `inference` выше и отдельная
    строка бюджета), а норматив §12 говорит о «времени поиска на базе до
    100 000 лиц» — то есть о той части, которая растёт с размером базы.
    """
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return {"skipped": "psycopg2 не установлен"}

    dsn = os.environ.get("BENCH_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        return {"skipped": "не задан DATABASE_URL/BENCH_DATABASE_URL"}
    # SQLAlchemy-DSN (postgresql+asyncpg://) psycopg2 не понимает.
    dsn = dsn.replace("+asyncpg", "").replace("+psycopg2", "")

    import psycopg2
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as e:
        return {"skipped": f"Postgres недоступен: {e}"}
    conn.autocommit = True

    out: dict = {"rows": rows}
    try:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
            row = cur.fetchone()
            out["pgvector"] = row[0] if row else "?"

        seeded = _seed_facesearch(conn, rows, log=log)
        qvec = "[" + ",".join(f"{x:.4f}" for x in seeded.pop("_query_vec")) + "]"
        out.update(seeded)

        with conn.cursor() as cur:
            cur.execute(f"SET search_path = {FACESEARCH_SCHEMA}, public")
            # Порог 0.4 — дефолт эндпоинта (threshold_form(0.4)), limit 100.
            params = {"vec": qvec, "threshold": 0.4, "limit": 100}
            for key, sql in (("production_query", FACESEARCH_SQL),
                             ("indexed_order_by", FACESEARCH_SQL_INDEXED)):
                out[key] = _time_query(cur, sql, params)
                cur.execute("EXPLAIN (FORMAT TEXT) " + sql, params)
                plan = "\n".join(r[0] for r in cur.fetchall())
                # Признак использования HNSW — сканирование именно по
                # индексу на `embedding`. Подстрока "Index Scan" сама по
                # себе не годится: в плане есть и подзапрос на сегмент со
                # своим обычным btree-индексом.
                out[key]["uses_hnsw"] = "Index Scan using face_events_embedding" in plan
                out[key]["seq_scan_on_faces"] = "Seq Scan on face_events" in plan
                out[key]["plan"] = plan
    finally:
        if not keep:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {FACESEARCH_SCHEMA} CASCADE")
        conn.close()
    return out


# --- поиск по архиву (SPEC §7: ≤ 5 с на 500 000 сегментов) ----------------

ARCHIVE_SCHEMA = "bench_archive"

# Объём задаётся **числом сегментов**, а не числом камер.
#
# §7 новой редакции ТЗ (Production NVR Edition) формулирует норматив
# именно так: «поиск по архиву ≤ 5 секунд на объёме до 500 000
# сегментов». До этого объём считался как «120 камер × 14 дней», но §1
# снял фиксированные 120 камер (объект — 12–250+), а §22 прямо запрещает
# такой хардкод: на объекте из 250 камер норматив достигался бы вдвое
# раньше, чем наступает заявленный предел.
#
# Число камер выводится из целевого объёма, а не наоборот. Сегменты по
# 5 минут — нижняя граница §5 (она даёт больше строк, то есть худший
# случай), retention 14 дней — значение по умолчанию.
ARCHIVE_SEGMENTS = 500_000
ARCHIVE_DAYS = 14
ARCHIVE_SEGMENT_SEC = 300
# События лиц идут только с камер analytics (§15: «только для камер в
# режиме analytics») — объём базы лиц берётся из норматива §15
# («≤ 3-5 с на базе до 100 000 лиц»).
ARCHIVE_ANALYTICS_CAMERAS = 2
ARCHIVE_FACES = 100_000


def _archive_cameras(segments: int = ARCHIVE_SEGMENTS) -> int:
    """Сколько камер писать, чтобы получить заданное число сегментов."""
    per_camera = ARCHIVE_DAYS * 86400 // ARCHIVE_SEGMENT_SEC
    return max(1, -(-segments // per_camera))  # округление вверх


def _archive_ddl() -> list[str]:
    """DDL, повторяющий models.py (включая одиночные индексы).

    Таблицы создаются вручную, а не через `Base.metadata.create_all`, чтобы
    бенчмарк не тянул asyncpg/pgvector и не зависел от инициализации
    приложения. Расхождение с моделью ловится тестом
    backend/tests/test_archive_bench_matches_model.py.
    """
    return [
        """CREATE TABLE video_segments (
             id serial PRIMARY KEY,
             camera_id integer NOT NULL,
             started_at timestamp NOT NULL,
             ended_at timestamp NOT NULL,
             file_path varchar(500) NOT NULL,
             event_type varchar(20) NOT NULL,
             duration_sec integer NOT NULL DEFAULT 0,
             size_bytes bigint NOT NULL DEFAULT 0)""",
        """CREATE TABLE face_events (
             id serial PRIMARY KEY,
             camera_id integer NOT NULL,
             person_id integer,
             ts timestamp NOT NULL,
             snapshot_path varchar(500),
             orig_snapshot_path varchar(500),
             enhanced boolean NOT NULL DEFAULT false,
             bbox json,
             is_known boolean NOT NULL DEFAULT false)""",
        "CREATE INDEX ix_video_segments_camera_id ON video_segments (camera_id)",
        "CREATE INDEX ix_video_segments_started_at ON video_segments (started_at)",
        "CREATE INDEX ix_video_segments_ended_at ON video_segments (ended_at)",
        "CREATE INDEX ix_face_events_camera_id ON face_events (camera_id)",
        "CREATE INDEX ix_face_events_person_id ON face_events (person_id)",
        "CREATE INDEX ix_face_events_ts ON face_events (ts)",
    ]


def _archive_queries():
    """Фильтры страницы «Видеоархив» — как их составляет интерфейс.

    Запросы собираются **production-функцией** `segments_query()` из
    backend/app/services/archive_query.py и компилируются в SQL. Копии SQL
    здесь нет намеренно: копия расходится с оригиналом молча (урок цикла 26),
    и тогда бенчмарк подтверждает норматив для запроса, которого в
    приложении уже нет.
    """
    import sys as _sys
    from pathlib import Path

    backend = Path(__file__).resolve().parents[1] / "backend"
    if str(backend) not in _sys.path:
        _sys.path.insert(0, str(backend))
    from sqlalchemy.dialects import postgresql
    from app.services.archive_query import segments_query  # noqa: E402

    def sql(**kw):
        q = segments_query(**kw)
        return str(q.compile(dialect=postgresql.dialect(),
                             compile_kwargs={"literal_binds": True}))

    # Даты берутся внутри окна засева (последние 14 дней).
    day3 = "CURRENT_TIMESTAMP - interval '3 days'"
    day2 = "CURRENT_TIMESTAMP - interval '2 days'"
    from datetime import datetime, timedelta
    now = datetime.utcnow()
    d_from, d_to = now - timedelta(days=3), now - timedelta(days=2)

    return [
        ("без фильтров", sql()),
        ("камера", sql(camera_id=77)),
        ("камера + сутки", sql(camera_id=77, date_from=d_from, date_to=d_to)),
        ("только даты", sql(date_from=d_from, date_to=d_to)),
        # Редкий event_type: строк нет вовсе, то есть отсев идёт по всей
        # таблице — худший случай для этого фильтра.
        ("тип события (редкий)", sql(event_type="face")),
        ("персона", sql(person_id=42)),
        ("персона + камера + сутки",
         sql(person_id=42, camera_id=1, date_from=d_from, date_to=d_to)),
    ]


def _seed_archive(conn, log=None, target_segments: int = ARCHIVE_SEGMENTS) -> dict:
    cameras = _archive_cameras(target_segments)
    segments = cameras * ARCHIVE_DAYS * (86400 // ARCHIVE_SEGMENT_SEC)
    t0 = time.perf_counter()
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {ARCHIVE_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {ARCHIVE_SCHEMA}")
        cur.execute(f"SET search_path = {ARCHIVE_SCHEMA}, public")
        for stmt in _archive_ddl():
            cur.execute(stmt)
        if log:
            log(f"  засев {segments} сегментов ({cameras} камер × "
                f"{ARCHIVE_DAYS} дней)...")
        last = ARCHIVE_DAYS * 86400 // ARCHIVE_SEGMENT_SEC - 1
        cur.execute(f"""
            INSERT INTO video_segments
                (camera_id, started_at, ended_at, file_path, event_type,
                 duration_sec, size_bytes)
            SELECT c,
                   now()::timestamp - interval '{ARCHIVE_DAYS} days'
                       + (s * interval '{ARCHIVE_SEGMENT_SEC} seconds'),
                   now()::timestamp - interval '{ARCHIVE_DAYS} days'
                       + ((s + 1) * interval '{ARCHIVE_SEGMENT_SEC} seconds'),
                   '/media/segments/cam' || c || '_' || s || '.mp4',
                   'continuous', {ARCHIVE_SEGMENT_SEC}, 75000000
            FROM generate_series(1, {cameras}) c,
                 generate_series(0, {last}) s
        """)
        if log:
            log(f"  засев {ARCHIVE_FACES} событий лиц...")
        step = max(1, ARCHIVE_DAYS * 86400 // ARCHIVE_FACES)
        cur.execute(f"""
            INSERT INTO face_events (camera_id, person_id, ts, snapshot_path, is_known)
            SELECT 1 + (i % {ARCHIVE_ANALYTICS_CAMERAS}),
                   1 + (i % 500),
                   now()::timestamp - interval '{ARCHIVE_DAYS} days'
                       + (i * interval '{step} seconds'),
                   '/media/faces/f' || i || '.jpg',
                   (i % 3) = 0
            FROM generate_series(1, {ARCHIVE_FACES}) i
        """)
        cur.execute("ANALYZE video_segments")
        cur.execute("ANALYZE face_events")
        cur.execute("SELECT pg_total_relation_size('video_segments')")
        size = cur.fetchone()[0]
    return {
        "segments": segments,
        "cameras": cameras,
        "faces": ARCHIVE_FACES,
        "table_mb": round(size / 1024 / 1024, 1),
        "seed_sec": round(time.perf_counter() - t0, 1),
    }


def bench_archive(keep: bool = False, log=None) -> dict:
    """Поиск по архиву на объёме норматива (SPEC §7: ≤ 5 с на 500 000 сегментов).

    Меряется запрос, а не весь эндпоинт: сериализация 200 строк не растёт
    с размером архива, а норматив говорит именно о поиске по нему.

    Смысл замера — не «уложились ли», а **на чём именно** уложились: время
    в норматив может влезать случайно, за счёт запаса железа, при плане с
    полным перебором (так поиск по фото 25 циклов «проходил», ни разу не
    задев HNSW).
    Поэтому рядом с миллисекундами печатается признак Seq Scan.
    """
    try:
        import psycopg2  # noqa: F401
    except ImportError:
        return {"skipped": "psycopg2 не установлен"}

    dsn = os.environ.get("BENCH_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        return {"skipped": "не задан DATABASE_URL/BENCH_DATABASE_URL"}
    dsn = dsn.replace("+asyncpg", "").replace("+psycopg2", "")

    try:
        queries = _archive_queries()
    except Exception as e:  # sqlalchemy/backend недоступны
        return {"skipped": f"не удалось собрать production-запрос: {e}"}

    import psycopg2
    try:
        conn = psycopg2.connect(dsn, connect_timeout=5)
    except Exception as e:
        return {"skipped": f"Postgres недоступен: {e}"}
    conn.autocommit = True

    out: dict = {"limit_sec": 5.0}
    try:
        out.update(_seed_archive(conn, log=log))
        with conn.cursor() as cur:
            cur.execute(f"SET search_path = {ARCHIVE_SCHEMA}, public")
            measured = {}
            for name, sql in queries:
                measured[name] = _time_query(cur, sql, {}, repeats=3)
                cur.execute("EXPLAIN (FORMAT TEXT) " + sql)
                plan = "\n".join(r[0] for r in cur.fetchall())
                measured[name]["seq_scan"] = "Seq Scan on video_segments" in plan
            out["queries"] = measured
            out["worst_ms"] = max(q["ms_mean"] for q in measured.values())
            out["within_limit"] = out["worst_ms"] <= out["limit_sec"] * 1000
    finally:
        if not keep:
            with conn.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {ARCHIVE_SCHEMA} CASCADE")
        conn.close()
    return out


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
        if "chain" in groups:
            if not _have("ffmpeg"):
                result["chain"] = {"skipped": "ffmpeg не найден"}
            else:
                face_clip = os.path.join(tmpdir, "faces.mp4")
                made = make_face_clip(face_clip)
                if "error" in made:
                    result["chain"] = {"skipped": made["error"]}
                else:
                    result["chain"] = bench_analytics_chain(face_clip)
                    result["chain"]["clip"] = made
        if "archive" in groups:
            result["archive"] = bench_archive(
                log=(None if os.environ.get("BENCH_QUIET")
                     else lambda m: print(m, file=sys.stderr)),
            )
        if "facesearch" in groups:
            result["facesearch"] = bench_facesearch(
                rows=int(os.environ.get("BENCH_FACES", FACESEARCH_ROWS)),
                # Прогресс засева идёт в stderr: stdout занят JSON'ом
                # для CI, и печать в него ломала разбор результата.
                log=(None if os.environ.get("BENCH_QUIET")
                     else lambda m: print(m, file=sys.stderr)),
            )
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
                    choices=["decode", "prefilter", "inference", "chain",
                             "facesearch", "archive"],
                    help="выполнить только указанные группы (можно повторять)")
    args = ap.parse_args()

    # facesearch и archive не входят в набор по умолчанию: они засевают
    # сотни тысяч строк и требуют живого Postgres — это минуты, а не
    # секунды. Запускаются явно (`--only facesearch`, `--only archive`),
    # в том числе из отдельной CI-джобы.
    # chain не входит в набор по умолчанию: он тянет модель buffalo_s из
    # сети (~125 МБ) и идёт минуты, а не секунды. Запускается явно
    # (`--only chain`) — как facesearch и archive.
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
    if "chain" in res:
        ch = res["chain"]
        print("\nПолная цепочка аналитики (SPEC §19: детекция ≥ 5 FPS/канал):")
        if "skipped" in ch:
            print(f"  пропущено: {ch['skipped']}")
        elif "error" in ch:
            print(f"  ОШИБКА: {ch['error']}")
        else:
            print(f"  модель {ch['model']}, det_size {ch['det_size']}, "
                  f"{'один поток' if ch['single_thread'] else 'все потоки'}")
            print(f"  {'decode':28} {ch['ms_decode']:8.2f} мс/кадр")
            print(f"  {'префильтр MOG2':28} {ch['ms_prefilter']:8.2f} мс/кадр")
            if ch["ms_detect_embed"] is not None:
                print(f"  {'детектор + эмбеддинг':28} {ch['ms_detect_embed']:8.2f} мс "
                      f"(p95 {ch['ms_detect_embed_p95']:.2f})")
            print(f"  найдено лиц: {ch['faces_detected']} за {ch['detector_runs']} "
                  f"прогонов детектора из {ch['frames']} кадров")
            verdict = "укладывается" if ch["meets_target"] else "НЕ УКЛАДЫВАЕТСЯ"
            print(f"  ИТОГО цепочка: {ch['chain_fps']:.2f} FPS/канал — {verdict} "
                  f"в норматив {ch['target_fps']:.0f} FPS")

    if "archive" in res:
        ar = res["archive"]
        print("\nПоиск по архиву (SPEC §7: ≤ 5 с на 500 000 сегментов):")
        if "skipped" in ar:
            print(f"  пропущено: {ar['skipped']}")
        else:
            print(f"  база: {ar['segments']} сегментов ({ar['table_mb']} МБ), "
                  f"{ar['faces']} событий лиц, засев {ar['seed_sec']} с")
            for name, q in ar["queries"].items():
                print(f"  {name:26} {q['ms_mean']:8.1f} мс  (p95 {q['ms_p95']:.1f}, "
                      f"строк {q['rows_returned']}"
                      f"{', Seq Scan' if q['seq_scan'] else ''})")
            verdict = "укладывается" if ar["within_limit"] else "НЕ УКЛАДЫВАЕТСЯ"
            print(f"  худший запрос: {ar['worst_ms']:.1f} мс — {verdict} "
                  f"в норматив {ar['limit_sec']} с")

    if "facesearch" in res:
        fs = res["facesearch"]
        print("\nПоиск похожих лиц (SPEC §12: ≤ 3–5 с на 100 000 лиц):")
        if "skipped" in fs:
            print(f"  пропущено: {fs['skipped']}")
        else:
            print(f"  база: {fs['rows']} эмбеддингов, pgvector {fs['pgvector']}")
            print(f"  засев {fs['seed_sec']} с, индекс HNSW {fs['index_build_sec']} с")
            for key, title in (("production_query", "запрос из search.py"),
                               ("indexed_order_by", "ORDER BY по оператору")):
                q = fs[key]
                print(f"  {title:24} {q['ms_mean']:8.1f} мс  (p95 {q['ms_p95']:.1f}, "
                      f"строк {q['rows_returned']}, HNSW: "
                      f"{'да' if q['uses_hnsw'] else 'НЕТ'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
