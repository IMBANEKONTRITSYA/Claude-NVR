"""Юнит-тест автоопределения аппаратного ускорения декодирования (ТЗ 18.2).
Не тянет cv2/onnxruntime — только чистая логика из hwaccel.py, как
backoff.py/shutdown.py (см. pure-logic worker job в CI)."""
import hwaccel


def test_hw_decode_requested_by_default(monkeypatch):
    monkeypatch.delenv("FACEWATCH_HW_DECODE", raising=False)
    assert hwaccel.hw_decode_requested() is True


def test_hw_decode_disabled_via_zero(monkeypatch):
    monkeypatch.setenv("FACEWATCH_HW_DECODE", "0")
    assert hwaccel.hw_decode_requested() is False


def test_hw_decode_disabled_via_false_case_insensitive(monkeypatch):
    monkeypatch.setenv("FACEWATCH_HW_DECODE", "False")
    assert hwaccel.hw_decode_requested() is False


def test_hw_decode_disabled_via_no(monkeypatch):
    monkeypatch.setenv("FACEWATCH_HW_DECODE", "no")
    assert hwaccel.hw_decode_requested() is False


def test_hw_decode_enabled_via_arbitrary_truthy_value(monkeypatch):
    monkeypatch.setenv("FACEWATCH_HW_DECODE", "1")
    assert hwaccel.hw_decode_requested() is True


def test_detect_name_reports_disabled_when_off(monkeypatch):
    monkeypatch.setenv("FACEWATCH_HW_DECODE", "0")
    assert "отключено" in hwaccel.detect_hw_accelerator_name()


def test_detect_name_prefers_nvidia_when_available(monkeypatch):
    monkeypatch.delenv("FACEWATCH_HW_DECODE", raising=False)
    monkeypatch.setattr(hwaccel.shutil, "which", lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    monkeypatch.setattr(hwaccel.os.path, "exists", lambda p: False)
    assert "NVDEC" in hwaccel.detect_hw_accelerator_name()


def test_detect_name_falls_back_to_vaapi_when_dri_present(monkeypatch):
    monkeypatch.delenv("FACEWATCH_HW_DECODE", raising=False)
    monkeypatch.setattr(hwaccel.shutil, "which", lambda name: None)
    monkeypatch.setattr(hwaccel.os.path, "exists", lambda p: p == "/dev/dri/renderD128")
    assert "VAAPI" in hwaccel.detect_hw_accelerator_name()


def test_detect_name_reports_no_hw_when_nothing_found(monkeypatch):
    monkeypatch.delenv("FACEWATCH_HW_DECODE", raising=False)
    monkeypatch.setattr(hwaccel.shutil, "which", lambda name: None)
    monkeypatch.setattr(hwaccel.os.path, "exists", lambda p: False)
    assert "не обнаружен" in hwaccel.detect_hw_accelerator_name()
