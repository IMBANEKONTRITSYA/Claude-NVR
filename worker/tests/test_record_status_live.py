"""Статус потоков против **настоящего** MediaMTX (SPEC §14, §9, §4).

Набор существует ровно потому, что схема API не отвечает на вопрос, ради
которого её читали. До цикла 43 `record_status.py` определял живость
потока по полю `online`, взятому из `api/openapi.yaml` v1.20.0. Поле в
схеме есть, тип у него `boolean`, юнит-тесты с ним зелёные — и всё это
время потеря потока не показывалась в интерфейсе, не меняла статус камеры
в БД и не давала алерта §14 **ни разу**: у пути со статическим источником
(а слой записи заводит только такие) `online` означает «путь заведён, и
сервер пытается тянуть», а не «поток идёт».

Здесь это проверяется единственным способом, который отличает одно от
другого, — обрывом настоящего потока на настоящем сервере.

Набор опт-ин: нужен `MEDIAMTX_BIN` и `ffmpeg`; в CI их готовит джоба
`record-layer-live`, при локальном прогоне набор пропускается.
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from record_layer import MediaMTXClient, path_conf, path_name, segments_dir
from record_status import ONLINE, OFFLINE, path_live, stream_states

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN")

pytestmark = pytest.mark.skipif(
    not MEDIAMTX_BIN or not os.path.exists(MEDIAMTX_BIN)
    or not shutil.which("ffmpeg"),
    reason="нужен настоящий MediaMTX (MEDIAMTX_BIN) и ffmpeg — см. джобу "
           "record-layer-live в .github/workflows/ci.yml",
)

CAMS = [(1, "Проходная")]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout: float, message: str, interval: float = 0.2):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if pred():
                return
        except Exception as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f"{message} (последняя ошибка: {last})")


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    media_root = tmp_path_factory.mktemp("archive")
    os.makedirs(segments_dir(str(media_root)), exist_ok=True)
    api_port, rtsp_port = _free_port(), _free_port()
    conf = tmp_path_factory.mktemp("conf") / "mediamtx.yml"
    conf.write_text(
        "logLevel: error\n"
        "api: yes\n"
        f"apiAddress: 127.0.0.1:{api_port}\n"
        f"rtspAddress: :{rtsp_port}\n"
        "rtmp: no\nhls: no\nwebrtc: no\nsrt: no\n"
        "pathDefaults:\n"
        "  record: no\n"
        "  recordPartDuration: 1s\n"
        "paths:\n"
        "  fakecam:\n"
        "    source: publisher\n",
        encoding="utf-8")

    mtx = subprocess.Popen([MEDIAMTX_BIN, str(conf)], cwd=str(conf.parent),
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    client = MediaMTXClient(f"http://127.0.0.1:{api_port}", timeout=5)
    _wait(lambda: client.list_path_configs() is not None, 20,
          "Control API не поднялся", interval=0.3)

    source = f"rtsp://127.0.0.1:{rtsp_port}/fakecam"
    client.add_path(path_name(1), path_conf(source, segment_duration_min=1,
                                            media_root=str(media_root)))
    camera = None
    try:
        camera = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", "15",
             "-pix_fmt", "yuv420p", "-f", "rtsp", "-rtsp_transport", "tcp",
             source],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait(lambda: path_live(client.runtime_paths().get(path_name(1))), 40,
              "запись не пошла")
        yield {"client": client, "camera": camera}
    finally:
        if camera is not None and camera.poll() is None:
            camera.terminate()
            try:
                camera.wait(timeout=10)
            except subprocess.TimeoutExpired:
                camera.kill()
        mtx.terminate()
        try:
            mtx.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mtx.kill()


def test_running_stream_is_online_and_counts_bytes(live):
    """Позитивный контроль: на живом потоке статус online, и объём
    ненулевой — на закреплённой v1.16.0 он приезжает под именем
    `bytesReceived`, которого прежний код не читал вовсе."""
    client = live["client"]
    assert stream_states(CAMS, client.runtime_paths())[1]["status"] == ONLINE
    # Счётчик обновляется не в тот же миг, что признак живости: путь
    # становится available, как только пришёл первый кадр, а байты сервер
    # сводит с задержкой. Ждать здесь обязательно — на загруженном раннере
    # мгновенная проверка ловит ноль и выглядит как несуществующий дефект.
    _wait(lambda: stream_states(CAMS, client.runtime_paths())[1]["inbound_bytes"] > 0,
          20, "объём принятого остался нулевым: в закреплённой версии поле "
              "называется bytesReceived, а не inboundBytes")
    assert stream_states(CAMS, client.runtime_paths())[1]["online_since"], (
        "«в сети с» не заполнено")


def test_lost_stream_is_reported_offline(live):
    """Дефект, из-за которого набор и написан: камера пропала, а слой
    записи продолжал показывать её онлайн — и так до конца суток.

    `online` при этом остаётся `true` (проверяется здесь же), поэтому тест
    сторожит не «какой-то признак жизни», а именно правильный.
    """
    client = live["client"]
    live["camera"].terminate()
    live["camera"].wait(timeout=10)

    _wait(lambda: not path_live(client.runtime_paths().get(path_name(1))), 30,
          "MediaMTX не отметил обрыв ни в available, ни в ready")
    runtime = client.runtime_paths()

    assert runtime[path_name(1)].get("online") is True, (
        "апстрим изменил семантику `online` — это хорошая новость, но "
        "комментарии в record_status.py надо переписать по факту")
    assert stream_states(CAMS, runtime)[1]["status"] == OFFLINE
