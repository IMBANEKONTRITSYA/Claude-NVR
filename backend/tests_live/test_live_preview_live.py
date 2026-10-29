"""Снимок камеры без аналитики — с настоящего MediaMTX, H.264 и H.265 (D04).

DEPLOY_FINDINGS D04 (2026-10-28): у 47 из 49 камер (record_only) снимка не
было вовсе — его писала только нить аналитики, — и плитка, не сумевшая
сыграть HLS, оставалась чёрной. Чаще всего HLS не играет на H.265, а H.265
на объекте у 91 из 98 потоков, поэтому проверяются оба кодека.

Главная проверка — что снимок является **кадром**, а не серым месивом.
Наивное `ffmpeg -frames:v 1` с RTSP начинает декодировать с P-кадра без
опорного и отдаёт JPEG равномерно-серого цвета (снято на MediaMTX 1.16.1,
H.265: «Could not find ref with POC …»). Файл при этом валидный, ответ 200,
и проверка «пришёл JPEG» была бы зелёной. Здесь измеряется разброс яркости:
у испытательной таблицы он большой, у серой каши — около нуля.
"""
import asyncio
import os
import shutil
import socket
import statistics
import subprocess
import time

import pytest

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN")
pytestmark = pytest.mark.skipif(
    not MEDIAMTX_BIN or not os.path.exists(MEDIAMTX_BIN) or not shutil.which("ffmpeg"),
    reason="нужны MEDIAMTX_BIN и ffmpeg (джоба CI record-layer-live)",
)

from app.config import settings  # noqa: E402
from app.services import live_preview  # noqa: E402

CAMS = {7: "h264", 8: "h265"}


def _mediamtx_env() -> dict:
    """Окружение без RTSP_*: MediaMTX читает их как свои настройки.

    Наследие rtsp-simple-server: переменная `RTSP_ENCRYPTION_KEY` (ключ
    шифрования учёток камер в окружении проекта) разбирается как настройка
    `encryption`, MediaMTX падает при старте с «RTSP_ENCRYPTION: json:
    Unmarshal(nil *conf.alias)», и тест видит лишь пустой путь. В пакете это
    не грозит — у юнита MediaMTX свой mediamtx.env (test_deb_packaging).
    """
    return {k: v for k, v in os.environ.items() if not k.startswith(("RTSP_", "MTX_"))}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _publisher(port: int, cam: int, codec: str) -> subprocess.Popen:
    enc = (["-c:v", "libx264", "-preset", "ultrafast", "-g", "15"] if codec == "h264" else
           # Закрытый GOP, как у IP-камер: ключевой кадр — IDR.
           ["-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "keyint=15:min-keyint=15:open-gop=0:repeat-headers=1:log-level=error"])
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc=size=640x360:rate=15", *enc, "-pix_fmt", "yuv420p",
         "-f", "rtsp", "-rtsp_transport", "tcp", f"rtsp://127.0.0.1:{port}/cam{cam}"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture(scope="module")
def mediamtx(tmp_path_factory):
    port = _free_port()
    conf = tmp_path_factory.mktemp("mtx") / "mediamtx.yml"
    conf.write_text(
        "logLevel: warn\napi: no\n"
        f"rtspAddress: :{port}\nrtspTransports: [tcp]\n"
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        "authInternalUsers:\n- user: any\n  permissions: [{action: read}, {action: publish}]\n"
        "paths:\n  all_others:\n", encoding="utf-8")
    mtx = subprocess.Popen([MEDIAMTX_BIN, str(conf)], cwd=str(conf.parent), env=_mediamtx_env(),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pubs = [_publisher(port, cam, codec) for cam, codec in CAMS.items()]
    time.sleep(4)  # публикаторы подключаются, первый GOP уходит в сервер
    try:
        yield port
    finally:
        for p in (*pubs, mtx):
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


@pytest.fixture()
def preview_env(mediamtx, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "MEDIA_PATH", str(tmp_path))
    monkeypatch.setattr(settings, "MEDIAMTX_HOST", "127.0.0.1")
    monkeypatch.setattr(settings, "MEDIAMTX_PORT", mediamtx)
    monkeypatch.setattr(live_preview, "_failed_at", {})
    monkeypatch.setattr(live_preview, "_locks", {})
    monkeypatch.setattr(live_preview, "_parallel", None)
    return tmp_path


def _luma_spread(jpeg: str) -> float:
    """Разброс яркости кадра: у таблицы — десятки, у серой каши — около нуля."""
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", jpeg, "-vf", "scale=64:36", "-f", "rawvideo",
         "-pix_fmt", "gray", "-"], capture_output=True, check=True).stdout
    return statistics.pstdev(raw)


@pytest.mark.parametrize("cam", sorted(CAMS), ids=lambda c: CAMS[c])
def test_snapshot_is_a_real_frame(preview_env, cam):
    t0 = time.monotonic()
    path = asyncio.run(live_preview.snapshot_for(cam))
    took = time.monotonic() - t0
    assert path == live_preview.preview_frame(cam), "снимок не снят"
    spread = _luma_spread(path)
    assert spread > 20, (
        f"{CAMS[cam]}: разброс яркости {spread:.1f} — это не кадр, а серое месиво "
        "(декодирование началось не с ключевого кадра)"
    )
    assert took < live_preview.GRAB_TIMEOUT_SEC


def test_second_request_is_served_from_cache(preview_env):
    """Плитки обновляют снимок раз в 10 с — повтор в пределах TTL без ffmpeg."""
    cam = 7
    first = asyncio.run(live_preview.snapshot_for(cam))
    mtime = os.path.getmtime(first)
    t0 = time.monotonic()
    again = asyncio.run(live_preview.snapshot_for(cam))
    assert again == first
    assert os.path.getmtime(again) == mtime, "кадр переснят внутри TTL"
    assert time.monotonic() - t0 < 0.5


def test_camera_without_stream_fails_fast_the_second_time(preview_env):
    """Камера без потока: первый раз — попытка, дальше — отказ из кэша."""
    cam = 99  # пути нет
    assert asyncio.run(live_preview.snapshot_for(cam)) is None
    t0 = time.monotonic()
    assert asyncio.run(live_preview.snapshot_for(cam)) is None
    assert time.monotonic() - t0 < 0.5, "отказ не закэширован — ffmpeg на каждый запрос"
