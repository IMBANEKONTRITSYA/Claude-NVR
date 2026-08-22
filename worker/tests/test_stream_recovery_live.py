"""Восстановление потока против **настоящего** MediaMTX (SPEC §19, §13).

Почему этот набор не может быть юнит-тестом. Всё, ради чего написан
`stream_recovery.py`, — это поведение чужого сервера:

* что путь без публикатора отвечает на DESCRIBE `404`, а с публикатором
  `200` (на этом стоит вся проба: TCP-проба ответила бы «жива» в обоих
  случаях, потому что RTSP-порт слушает сам медиасервер);
* что `delete` + `add` действительно перезапускает подключение к
  источнику — в отличие от `/v3/config/paths/replace` той же
  конфигурацией, который цикл 40 измерил и отверг (19 пинков за обрыв,
  восстановление те же 6.54 с);
* что после пересоздания пути запись **возобновляется**, а не открывается
  пустой файл, который никогда не растёт.

Двойник Control API из `test_stream_recovery.py` не знает ни одного из
этих фактов — он умеет отвечать на запросы конфигурации, а не терять и
восстанавливать RTSP-сессию. Ровно поэтому в проекте уже дважды зеленел
тест на свойство, которого на настоящем сервере не было (циклы 30 и 34,
см. `test_record_layer_live.py`).

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

from record_layer import (MediaMTXClient, path_conf, path_name, segments_dir,
                          sync_paths)
from record_status import path_live
from stream_recovery import (RecoveryPlanner, probe_many, recover_once,
                             rtsp_alive)

ROOT = Path(__file__).resolve().parents[2]
MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN")

pytestmark = pytest.mark.skipif(
    not MEDIAMTX_BIN or not os.path.exists(MEDIAMTX_BIN)
    or not shutil.which("ffmpeg"),
    reason="нужен настоящий MediaMTX (MEDIAMTX_BIN) и ffmpeg — см. джобу "
           "record-layer-live в .github/workflows/ci.yml",
)

SEGMENT_MIN = 1


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(pred, timeout: float, message: str, interval: float = 0.1):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            if pred():
                return time.monotonic()
        except Exception as exc:
            last = exc
        time.sleep(interval)
    raise AssertionError(f"{message} (последняя ошибка: {last})")


class _Camera:
    """ffmpeg в роли камеры: публикует 720p в путь `fakecam`.

    Обрыв и возвращение — это остановка и повторный запуск процесса, то
    есть ровно то, что происходит на объекте при перезагрузке камеры, с
    точки зрения медиасервера.
    """

    def __init__(self, rtsp_port: int):
        self.rtsp_port = rtsp_port
        self.proc: subprocess.Popen | None = None

    def start(self):
        self.proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=15",
             "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
             "-g", "15", "-pix_fmt", "yuv420p",
             "-f", "rtsp", "-rtsp_transport", "tcp",
             f"rtsp://127.0.0.1:{self.rtsp_port}/fakecam"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self

    def stop(self):
        if self.proc is None:
            return
        self.proc.terminate()
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc = None


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

    camera = _Camera(rtsp_port)
    try:
        yield {"client": client, "media_root": str(media_root), "camera": camera,
               "source": f"rtsp://127.0.0.1:{rtsp_port}/fakecam"}
    finally:
        camera.stop()
        mtx.terminate()
        try:
            mtx.wait(timeout=10)
        except subprocess.TimeoutExpired:
            mtx.kill()


def _written(seg_dir: str) -> int:
    total = 0
    for name in os.listdir(seg_dir):
        try:
            total += os.path.getsize(os.path.join(seg_dir, name))
        except OSError:
            pass
    return total


def _desired(live) -> dict:
    return {path_name(1): path_conf(live["source"],
                                    segment_duration_min=SEGMENT_MIN,
                                    media_root=live["media_root"])}


def test_probe_tells_a_live_stream_from_a_dead_one(live):
    """Основание всей схемы: DESCRIBE различает «поток есть» и «потока
    нет» там, где TCP-проба ответила бы «жива» всегда — порт слушает сам
    медиасервер, а не камера."""
    url = live["source"]
    assert rtsp_alive(url, timeout=3.0) is False, (
        "путь без публикатора обязан выглядеть мёртвым — иначе супервизор "
        "будет пересоздавать путь по молчащей камере")

    live["camera"].start()
    _wait(lambda: rtsp_alive(url, timeout=3.0) is True, 30,
          "проба не увидела появившийся поток")

    live["camera"].stop()
    _wait(lambda: rtsp_alive(url, timeout=3.0) is False, 30,
          "проба не увидела пропавший поток")


def test_nonblocking_pass_agrees_with_the_single_probe_on_a_real_server(live):
    """Неблокирующий проход (боевой с цикла 48) обязан отвечать то же
    самое, что и одиночная проба, — на НАСТОЯЩЕМ медиасервере.

    Локальные двойники из `test_stream_recovery.py` отвечают мгновенно и
    ровно то, что им велено; здесь на другом конце MediaMTX 1.16.0 со
    своим порядком ответов, своим SDP и своим поведением на пути без
    публикатора. Расхождение между двумя пробами означало бы, что
    супервизор в боевом режиме видит не то же, что видят тесты.
    """
    url = live["source"]
    targets = [("cam1", url)]
    assert dict(probe_many(targets, timeout=3.0)) == {"cam1": False}

    live["camera"].start()
    _wait(lambda: dict(probe_many(targets, timeout=3.0))["cam1"] is True, 30,
          "неблокирующий проход не увидел появившийся поток")
    assert rtsp_alive(url, timeout=3.0) is True, "пробы разошлись на живом потоке"

    live["camera"].stop()
    _wait(lambda: dict(probe_many(targets, timeout=3.0))["cam1"] is False, 30,
          "неблокирующий проход не увидел пропавший поток")


def test_kick_restores_recording_faster_than_the_server_would(live):
    """Главное свойство: после возвращения камеры запись возобновляется в
    бюджете §19 (≤ 5 с), а не через собственную паузу MediaMTX в 5 с плюс
    подключение (замер цикла 40 — 6.6 с худшее).

    Замеряется то же, что и в `perf/bench_recovery.py`: от момента, когда
    источник снова опубликован, до момента, когда файл снова растёт.
    Здесь это тест, а не бенчмарк, поэтому порог взят с запасом на
    медленный раннер — бюджет §19 целиком; точные числа снимает бенчмарк.
    """
    seg_dir = segments_dir(live["media_root"])
    client, planner = live["client"], RecoveryPlanner()

    live["camera"].start()
    sync_paths(client, _desired(live))
    _wait(lambda: path_live(client.runtime_paths().get(path_name(1))),
          40, "запись не пошла до обрыва")
    base = _written(seg_dir)
    _wait(lambda: _written(seg_dir) > base, 40, "файл не рос до обрыва")

    live["camera"].stop()
    # Живость — по `available`/`ready`: `online` у статического
    # источника не падает на обрыве никогда (см. record_status.py).
    _wait(lambda: not path_live(client.runtime_paths().get(path_name(1))),
          30, "MediaMTX не заметил обрыв")

    # Пока камеры нет, супервизор обязан молчать: пинок по молчащей камере
    # ничего не чинит, а конфигурацию рвёт.
    stats = recover_once(client, _desired(live), client.runtime_paths(), planner)
    assert stats["kicked"] == 0, "пинок ушёл по камере, которой нет"

    live["camera"].start()
    t_source = _wait(
        lambda: path_live(client.runtime_paths().get("fakecam")), 40,
        "камера не вернулась")

    # Супервизор в его боевом режиме: опрос по расписанию, пинок по факту
    # ответа. Цикл здесь играет роль нити RecoverySupervisor.
    before = _written(seg_dir)
    kicked = False
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        stats = recover_once(client, _desired(live), client.runtime_paths(), planner)
        kicked = kicked or bool(stats["kicked"])
        if _written(seg_dir) > before:
            break
        time.sleep(0.1)
    t_record = _wait(lambda: _written(seg_dir) > before, 20,
                     "запись не возобновилась после возвращения камеры")

    assert kicked, "супервизор не пересоздал путь по ответившей камере"
    recovery = t_record - t_source
    assert recovery <= 5.0, (
        f"восстановление {recovery:.2f} с — норматив §19 (5 с) не выполнен "
        f"даже с супервизором")
