"""Обрыв камеры виден в архиве как разрыв, а не как «покрытое» время (D05).

DEPLOY_FINDINGS D05 (развёртывание 2026-10-28, 49 камер): после сбоя
сегмент с 7.6 с видео числился в архиве 13-секундным — конец брался как
начало следующего файла. Шкала не показывала разрыва, мониторинг писал
«Пропусков записи: 0», экспорт окна через разрыв отдавал 5.8 с вместо 10
при ответе 200.

Здесь — тот же сценарий на настоящем MediaMTX: запись идёт, источник
пропадает, возвращается; индексация обязана дать первому файлу длительность
видео, которое в нём есть, и оставить между файлами разрыв. Двойник файлов
с подставленным mtime этого не проверил бы: дефект сидел именно в том, как
настоящий сервер закрывает файл при обрыве и при ротации.
"""
import os
import shutil
import socket
import subprocess
import time

import pytest

from record_layer import (
    SEGMENT_JOIN_TOLERANCE_SEC,
    collect_complete_segments,
    parse_segment_name,
    probe_media_duration,
    segments_dir,
)

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN")

pytestmark = pytest.mark.skipif(
    not MEDIAMTX_BIN or not os.path.exists(MEDIAMTX_BIN)
    or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="нужны MEDIAMTX_BIN, ffmpeg и ffprobe (джоба CI record-layer-live)",
)

OUTAGE_SEC = 8.0


def _mediamtx_env() -> dict:
    """Без RTSP_*: MediaMTX читает их как свои настройки, и RTSP_ENCRYPTION_KEY
    из окружения проекта роняет его при старте («RTSP_ENCRYPTION: json:
    Unmarshal(nil *conf.alias)»)."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("RTSP_", "MTX_"))}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout: float, message: str):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return
        time.sleep(0.2)
    pytest.fail(message)


def _publisher(rtsp_port: int) -> subprocess.Popen:
    # GOP 1 с: MediaMTX режет файлы по ключевым кадрам, и с редкими
    # ключевыми кадрами граница сдвигалась бы на секунды.
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-g", "15", "-pix_fmt", "yuv420p",
         "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{rtsp_port}/src"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)


def _stop(proc):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def outage(tmp_path_factory):
    """Запись с обрывом источника посередине; отдаёт каталог сегментов.

    Сегмент штатной ротации задан на час: в файле заведомо нет ни одной
    ротации, и закрыться досрочно он может только из-за обрыва.
    """
    media_root = str(tmp_path_factory.mktemp("archive"))
    seg_dir = segments_dir(media_root)
    os.makedirs(seg_dir, exist_ok=True)
    rtsp_port = _free_port()
    conf = tmp_path_factory.mktemp("conf") / "mediamtx.yml"
    conf.write_text(
        "logLevel: warn\napi: no\n"
        f"rtspAddress: :{rtsp_port}\n"
        # Только TCP: UDP-транспорт занял бы пару портов RTP/RTCP (чётный и
        # следующий), а свободную такую пару случайным выбором не найти.
        "rtspTransports: [tcp]\n"
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        "paths:\n"
        "  src:\n    source: publisher\n"
        "  cam7:\n"
        f"    source: rtsp://127.0.0.1:{rtsp_port}/src\n"
        "    rtspTransport: tcp\n"
        "    record: yes\n    recordFormat: fmp4\n"
        f"    recordPath: {seg_dir}/%path_%s\n"
        "    recordPartDuration: 1s\n    recordSegmentDuration: 1h\n",
        encoding="utf-8")
    mtx = subprocess.Popen([MEDIAMTX_BIN, str(conf)], cwd=str(conf.parent), env=_mediamtx_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pub = _publisher(rtsp_port)
    try:
        files = lambda: sorted(f for f in os.listdir(seg_dir) if parse_segment_name(f))
        _wait(lambda: files() and os.path.getsize(os.path.join(seg_dir, files()[0])) > 50_000,
              40, "запись не началась")
        time.sleep(5)                       # первые секунды видео
        _stop(pub)                          # камера пропала
        time.sleep(OUTAGE_SEC)
        pub = _publisher(rtsp_port)         # камера вернулась
        _wait(lambda: len(files()) >= 2 and os.path.getsize(os.path.join(seg_dir, files()[1])) > 50_000,
              60, "после возвращения источника второй сегмент не появился")
        time.sleep(3)
    finally:
        _stop(pub)
        _stop(mtx)
    yield seg_dir


def _indexed(seg_dir, **kw):
    return collect_complete_segments(seg_dir, now=time.time() + 3600, **kw)


def test_closed_segment_ends_where_its_video_ends(outage):
    first, second = _indexed(outage)[:2]
    real = probe_media_duration(first["file_path"])
    assert real is not None, "ffprobe не разобрал сегмент — проверять нечем"
    indexed = first["ended_ts"] - first["started_ts"]
    assert abs(indexed - real) < 1.0, (
        f"в архиве {indexed:.2f} с, видео в файле {real:.2f} с — "
        "обрыв засчитан закрытому файлу"
    )


def test_outage_remains_a_gap_between_segments(outage):
    """То, ради чего всё: шкала, контроль пропусков и экспорт видят разрыв."""
    first, second = _indexed(outage)[:2]
    gap = second["started_ts"] - first["ended_ts"]
    assert gap > SEGMENT_JOIN_TOLERANCE_SEC, (
        f"между сегментами {gap:.2f} с при обрыве ~{OUTAGE_SEC:.0f} с — "
        "разрыв в архиве не виден"
    )


def test_without_ffprobe_the_bound_is_still_the_last_write(outage):
    """Нет ffprobe — граница по mtime: завышение на часть, а не на обрыв."""
    first, second = _indexed(outage, probe=lambda _path: None)[:2]
    gap = second["started_ts"] - first["ended_ts"]
    assert gap > SEGMENT_JOIN_TOLERANCE_SEC, f"без ffprobe разрыв пропал: {gap:.2f} с"
