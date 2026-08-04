"""Юнит-тест сборки ffmpeg-аргументов транскода архива (ТЗ 18.8: настраиваемый
битрейт H.265/H.264, запись только ключевых кадров). Не тянет тяжёлые
ML-зависимости воркера (cv2/insightface) — только чистые функции из
record_encode.py."""
from record_encode import build_quality_args, build_gop_args, build_encode_args


def test_zero_bitrate_uses_crf():
    assert build_quality_args(0) == ["-crf", "23"]


def test_positive_bitrate_sets_capped_rate_control():
    args = build_quality_args(4000)
    assert args == ["-b:v", "4000k", "-maxrate", "4000k", "-bufsize", "8000k"]


def test_negative_bitrate_falls_back_to_crf():
    assert build_quality_args(-1) == ["-crf", "23"]


def test_gop_args_empty_when_iframe_only_disabled():
    assert build_gop_args(False) == []


def test_gop_args_forces_every_frame_keyframe_when_enabled():
    assert build_gop_args(True) == ["-g", "1", "-bf", "0"]


def test_build_encode_args_combines_codec_quality_and_gop():
    args = build_encode_args("libx265", bitrate_kbps=2000, iframe_only=True)
    assert args == [
        "-c:v", "libx265", "-preset", "veryfast",
        "-b:v", "2000k", "-maxrate", "2000k", "-bufsize", "4000k",
        "-g", "1", "-bf", "0",
    ]


def test_build_encode_args_default_crf_no_gop():
    args = build_encode_args("libx264")
    assert args == ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
