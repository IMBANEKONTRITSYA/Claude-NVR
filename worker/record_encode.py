"""Сборка аргументов ffmpeg для транскода архивных сегментов (ТЗ 18.8:
H.265, настраиваемый битрейт, запись только ключевых кадров).

Чистая функция без сети/файлов/тяжёлых ML-зависимостей — тестируется в CI
отдельно от worker.py (который тянет cv2/insightface, недоступные в CI
worker-job), тем же приёмом, что backoff.py и onvif_client.py."""


def build_quality_args(bitrate_kbps: int) -> list[str]:
    """bitrate_kbps=0 — CRF-режим (авто-качество, переменный размер файла).
    bitrate_kbps>0 — фиксированный потолок битрейта (предсказуемый размер
    архива для планирования места на диске)."""
    if bitrate_kbps and bitrate_kbps > 0:
        return [
            "-b:v", f"{bitrate_kbps}k",
            "-maxrate", f"{bitrate_kbps}k",
            "-bufsize", f"{bitrate_kbps * 2}k",
        ]
    return ["-crf", "23"]


def build_gop_args(iframe_only: bool) -> list[str]:
    """iframe_only=True — каждый кадр ключевой (GOP=1, без B-кадров):
    максимальная экономия места ценой худшего сжатия по сравнению с обычным
    GOP-кодированием."""
    return ["-g", "1", "-bf", "0"] if iframe_only else []


def build_encode_args(codec: str, bitrate_kbps: int = 0, iframe_only: bool = False) -> list[str]:
    """Полный набор видео-аргументов ffmpeg (без -i/-y/выходного пути)."""
    return ["-c:v", codec, "-preset", "veryfast",
            *build_quality_args(bitrate_kbps), *build_gop_args(iframe_only)]
