"""Автоопределение аппаратного ускорения ДЕКОДИРОВАНИЯ видео (ТЗ 18.2).

Инференс нейросетей уже автоопределяет ускоритель (CUDA/DirectML/OpenVINO,
см. `detect_providers()` в worker.py) — этого модуля не хватало для второй
половины ТЗ 18.2: декодирования самих RTSP-кадров через GPU (Intel
QuickSync, NVIDIA NVDEC, AMD VCE/VAAPI).

Специально вынесено в отдельный модуль без импорта cv2/onnxruntime — как
`backoff.py`/`shutdown.py`, тестируется в CI-джобе "pure-logic worker tests"
без тяжёлых ML-зависимостей (см. worker/tests/test_hwaccel.py).

Фактический выбор бэкенда декодирования делает сам OpenCV/ffmpeg через
`cv2.VIDEO_ACCELERATION_ANY` (см. `open_capture()` в worker.py) — это
единственный режим, который одновременно (а) пробует доступный HW-ускоритель
и (б) прозрачно откатывается на программное декодирование, если ничего не
найдено, без падения открытия потока. Функции этого модуля — только для
диагностики (что реально есть в системе) и для флага полного отключения.
"""
import os
import shutil


def hw_decode_requested() -> bool:
    """FACEWATCH_HW_DECODE=0 отключает попытку HW-декодирования (форсирует
    программное) — например, если в конкретном окружении HW-путь ffmpeg
    ломается на камере с нестандартным кодеком. По умолчанию включено —
    ТЗ 18.2 требует автоопределения ускорителя, не ручной настройки."""
    return os.environ.get("FACEWATCH_HW_DECODE", "1").strip().lower() not in ("0", "false", "no", "")


def detect_hw_accelerator_name() -> str:
    """Best-effort определение, какой аппаратный ускоритель декодирования
    вероятно доступен в системе — только для лога/статуса при старте
    камеры. Не влияет на то, что реально выберет ffmpeg внутри
    VIDEO_ACCELERATION_ANY — та проба честнее любой эвристики отсюда,
    но проходит внутри OpenCV без возможности залогировать результат."""
    if not hw_decode_requested():
        return "отключено (FACEWATCH_HW_DECODE=0), программное декодирование"
    if shutil.which("nvidia-smi"):
        return "вероятно NVDEC (обнаружен nvidia-smi)"
    if any(os.path.exists(p) for p in ("/dev/dri/renderD128", "/dev/dri/card0")):
        return "вероятно VAAPI/QuickSync (обнаружен /dev/dri)"
    return "аппаратный ускоритель не обнаружен, программное декодирование"
