"""Потолок CPU слоя аналитики (SPEC §16, §19).

Две половины, как в остальном воркере: арифметика бюджета проверяется
чистыми тестами (идут в лёгкой CI-джобе), а то, что потолок реально
доезжает до сессии ONNX Runtime, — тестом на настоящей модели, который без
insightface аккуратно пропускается.

Вторая половина здесь важнее первой. Ограничение уже один раз выглядело
работающим, не работая: бенчмарк цикла 29 выставлял `OMP_NUM_THREADS=1`,
писал в отчёт «один поток — честное число на канал» и мерил при этом 3.97
занятых ядра. Проверять надо занятые ядра, а не наличие настройки.
"""
import os

import pytest

from ort_threads import (MAX_MACHINE_SHARE, MAX_THREADS_PER_CAMERA,
                         analytics_thread_budget)


# --- арифметика бюджета -----------------------------------------------

def test_manual_value_wins_over_arithmetic():
    """Значение из настроек главнее расчёта: у админа есть «Мониторинг»."""
    assert analytics_thread_budget(cores=64, analytics_cameras=2, configured=7) == 7


def test_manual_value_capped_by_cores():
    """Пул больше числа ядер не ускоряет ничего."""
    assert analytics_thread_budget(cores=4, analytics_cameras=1, configured=64) == 4


def test_auto_never_returns_zero():
    """Ноль означал бы «ORT решает сам» — то есть пул по числу ядер."""
    # Камер больше, чем отведённых слою ядер: деление даёт 0.
    assert analytics_thread_budget(cores=2, analytics_cameras=16) == 1


def test_auto_splits_layer_share_between_cameras():
    """Слою отводится половина машины, она делится на камеры."""
    # 64 потока → слою 32 → на 16 камер по 2, но не больше потолка.
    assert analytics_thread_budget(cores=64, analytics_cameras=16) == 2
    # 16 ядер → слою 8 → на 8 камер по одному.
    assert analytics_thread_budget(cores=16, analytics_cameras=8) == 1


def test_auto_respects_per_camera_ceiling():
    """Одна камера на большом сервере не забирает всё.

    Именно этот случай (много ядер, мало камер) без потолка и давал 3.09
    FPS на ядро вместо 9.75.
    """
    assert analytics_thread_budget(cores=64, analytics_cameras=1) == MAX_THREADS_PER_CAMERA


def test_layer_never_exceeds_its_share_of_the_machine():
    """Свойство, ради которого модуль существует (§2, §19).

    Проверяется на сетке, а не на одном примере: слой аналитики
    (камеры × потоки) не должен превышать отведённую ему долю машины,
    пока камер не больше, чем ядер в этой доле.
    """
    for cores in (2, 4, 8, 16, 32, 64, 128):
        share = int(cores * MAX_MACHINE_SHARE)
        for cameras in range(1, max(2, share) + 1):
            threads = analytics_thread_budget(cores, cameras)
            assert cameras * threads <= max(share, cameras), (
                f"{cores} ядер, {cameras} камер: {cameras}×{threads} "
                f"превышает долю слоя {share}"
            )


def test_target_server_leaves_room_for_recording_layer():
    """Целевой сервер §20: 64 потока, 3 камеры аналитики.

    Слою записи по §16 нужно 0.04 ядра на камеру — на 120 камерах это
    4.8 ядра, и они обязаны остаться свободными.
    """
    threads = analytics_thread_budget(cores=64, analytics_cameras=3)
    analytics_cores = 3 * threads
    assert analytics_cores <= 8
    assert 64 - analytics_cores > 120 * 0.04 + 2   # запись + приложение


# --- потолок доезжает до ONNX Runtime ---------------------------------

def _cores_busy(threads: int, frames: int = 12) -> float:
    """Сколько ядер реально занимает инференс при `threads` потоках."""
    import time

    import numpy as np
    from insightface.app import FaceAnalysis

    import ort_threads

    ort_threads.limit_threads(threads)
    try:
        app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"],
                           allowed_modules=["detection", "recognition"])
        app.prepare(ctx_id=0, det_size=(640, 640))
        # Шум, а не нули: на пустом кадре детектор выходит раньше, и замер
        # мерил бы возврат из функции, а не инференс.
        frame = np.random.randint(0, 255, (720, 1280, 3), dtype=np.uint8)
        app.get(frame)                     # прогрев
        cpu0, wall0 = time.process_time(), time.perf_counter()
        for _ in range(frames):
            app.get(frame)
        cpu = time.process_time() - cpu0
        wall = time.perf_counter() - wall0
    finally:
        ort_threads.restore()
    return cpu / max(wall, 1e-9)


@pytest.mark.skipif(os.environ.get("ORT_THREAD_LIMIT_TEST") != "1",
                    reason="тяжёлый замер на настоящей модели: "
                           "ORT_THREAD_LIMIT_TEST=1")
def test_limit_actually_bounds_cpu():
    """Один поток — одно занятое ядро, а не «настройка выставлена».

    Тест намеренно смотрит на занятые ядра: проверка «в SessionOptions
    лежит 1» прошла бы и на `OMP_NUM_THREADS`, который на самом деле ни
    на что не влияет.
    """
    pytest.importorskip("insightface")
    pytest.importorskip("onnxruntime")
    busy = _cores_busy(1)
    assert busy < 1.6, f"при одном потоке занято {busy:.2f} ядра"


def test_patch_is_idempotent_and_reversible():
    """Повторный вызов не наслаивает подмену, restore() её снимает."""
    mz = pytest.importorskip("insightface.model_zoo.model_zoo")
    import ort_threads

    original = mz.PickableInferenceSession
    assert ort_threads.limit_threads(1) is True
    first = mz.PickableInferenceSession
    assert first is not original
    assert ort_threads.limit_threads(2) is True
    # Второй вызов наследуется от ИСХОДНОГО класса, а не от первой подмены:
    # иначе после десяти перезагрузок модели (смена профиля в админке —
    # штатное действие) вырастала бы башня из десяти наследников.
    assert mz.PickableInferenceSession.__bases__ == (original,)
    ort_threads.restore()
    assert mz.PickableInferenceSession is original
