"""Слой записи против **настоящего** MediaMTX, а не против двойника — цикл 39.

Почему это отдельный набор, а не расширение `test_record_layer.py`.
Двойник Control API из того модуля уже дважды молча разошёлся с настоящим
сервером, и оба раза расхождение находил не он, а ручной запуск бинарника:

* цикл 30 — MediaMTX отдаёт длительности в форме `5m0s`, а не в той, что
  прислали; пока двойник возвращал присланное, `test_sync_is_idempotent`
  проходил, а на боевом сервере синхронизация патчила **каждый** путь на
  каждом тике менеджера;
* цикл 34 — v1.16.0 отдаёт нулевую длительность **пустой строкой**, а не
  `0s`, как v1.9.3; тот же тест снова проходил, а `recordDeleteAfter: 0s`
  снова давал бесконечный поток PATCH-запросов.

Оба раза свойство, которое тест «подтверждал», в реальности отсутствовало.
Третий раз ловить это вручную смысла нет: MediaMTX — статический бинарник,
он скачивается и запускается и в песочнице, и на раннере CI, без Docker.

Набор опт-ин: нужен `MEDIAMTX_BIN` (путь к бинарнику) и `ffmpeg`. В CI их
готовит джоба `record-layer-live`; при локальном прогоне набор
пропускается, а не падает.

Версия бинарника обязана совпадать с той, что зафиксирована в
`docker-compose.yml` — иначе проверяется не то, что поедет на объект;
`test_binary_matches_the_pinned_version` это стережёт.
"""
import os
import re
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from record_layer import (MediaMTXClient, collect_complete_segments,
                          parse_segment_name, path_conf, path_name,
                          record_path_template, segments_dir, sync_paths)

ROOT = Path(__file__).resolve().parents[2]
MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN")

pytestmark = pytest.mark.skipif(
    not MEDIAMTX_BIN or not os.path.exists(MEDIAMTX_BIN)
    or not shutil.which("ffmpeg"),
    reason="нужен настоящий MediaMTX (MEDIAMTX_BIN) и ffmpeg — см. джобу "
           "record-layer-live в .github/workflows/ci.yml",
)

# Один сегмент боевой длительности — 5 минут (SPEC §20), ждать его в тесте
# нельзя. Берётся минимальная длительность, которую принимает path_conf, а
# «дописан ли сегмент» проверяется с укороченным settle: и то, и другое —
# параметры замера, а не поведения, и подменяются явно.
SEGMENT_MIN = 1
SETTLE_SEC = 2.0


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pinned_version() -> str:
    """Версия MediaMTX из docker-compose.yml — единственный источник."""
    text = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    m = re.search(r"image:\s*bluenviron/mediamtx:(\S+)", text)
    assert m, "в docker-compose.yml не найден образ MediaMTX"
    return m.group(1).strip()


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    """Настоящий MediaMTX + настоящий RTSP-источник.

    Источник — ffmpeg, публикующий синтетический поток в путь `fakecam`
    того же сервера. Так у слоя записи появляется камера, которую он тянет
    ровно тем же способом, что и настоящую: `source: rtsp://…`,
    `sourceOnDemand: false`.
    """
    media_root = tmp_path_factory.mktemp("archive")
    os.makedirs(segments_dir(str(media_root)), exist_ok=True)
    api_port, rtsp_port = _free_port(), _free_port()
    conf = tmp_path_factory.mktemp("conf") / "mediamtx.yml"
    conf.write_text(
        "logLevel: warn\n"
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
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    client = MediaMTXClient(f"http://127.0.0.1:{api_port}", timeout=5)
    _wait(lambda: client.list_path_configs() is not None, 20,
          "Control API не поднялся")

    cam = subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
         "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=15",
         "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
         "-g", "15", "-pix_fmt", "yuv420p",
         "-f", "rtsp", "-rtsp_transport", "tcp",
         f"rtsp://127.0.0.1:{rtsp_port}/fakecam"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _wait(lambda: any(p.get("ready") for p in client.runtime_paths().values()),
          25, "источник не начал публиковаться")

    try:
        yield {"client": client, "media_root": str(media_root),
               "source": f"rtsp://127.0.0.1:{rtsp_port}/fakecam"}
    finally:
        for proc in (cam, mtx):
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _wait(pred, timeout: float, message: str, interval: float = 0.3):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            if pred():
                return
        except Exception as exc:  # сервер ещё не слушает — это ожидаемо
            last = exc
        time.sleep(interval)
    raise AssertionError(f"{message} (последняя ошибка: {last})")


def test_binary_matches_the_pinned_version():
    """Проверять надо ту версию, которая поедет на объект. Разъедется
    docker-compose с джобой — проверка станет бесполезной молча."""
    out = subprocess.run([MEDIAMTX_BIN, "--version"],
                         capture_output=True, text=True).stdout.strip()
    assert out.lstrip("v") == _pinned_version().lstrip("v"), (
        f"бинарник {out}, а docker-compose.yml закрепляет {_pinned_version()}")


def test_path_is_created_on_the_real_control_api(live):
    """Базовое: наш клиент заводит путь, и настоящий сервер его принимает."""
    desired = {path_name(1): path_conf(live["source"],
                                       segment_duration_min=SEGMENT_MIN,
                                       media_root=live["media_root"])}
    stats = sync_paths(live["client"], desired)

    assert stats["added"] == 1 and stats["failed"] == 0, stats
    assert path_name(1) in live["client"].list_path_configs()


def test_sync_is_idempotent_against_the_real_server(live):
    """Свойство, которое двойник подтверждал ложно **дважды** (циклы 30,
    34). Второй проход по неизменившемуся списку не должен писать ничего:
    иначе менеджер патчит все пути каждые ~10 секунд, а на объекте это
    сотни лишних запросов в минуту и лог, в котором ничего не найти."""
    desired = {path_name(1): path_conf(live["source"],
                                       segment_duration_min=SEGMENT_MIN,
                                       media_root=live["media_root"])}
    sync_paths(live["client"], desired)

    stats = sync_paths(live["client"], desired)

    assert stats == {"added": 0, "updated": 0, "deleted": 0, "failed": 0}, (
        "настоящий MediaMTX вернул конфигурацию в другой форме, и сверка "
        "сочла путь изменившимся")


def test_segments_land_in_the_configured_media_root(live):
    """Главная проверка цикла 39 на настоящем сервере: файлы появляются по
    настроенному `MEDIA_PATH`, а не по захардкоженному `/media/segments`.

    Проверяется именно то, чего не мог показать ни один юнит-тест: что
    MediaMTX понимает наш шаблон `recordPath` так же, как мы."""
    desired = {path_name(1): path_conf(live["source"],
                                       segment_duration_min=SEGMENT_MIN,
                                       media_root=live["media_root"])}
    sync_paths(live["client"], desired)
    seg_dir = segments_dir(live["media_root"])

    _wait(lambda: os.listdir(seg_dir), 30, "сегменты не появились")

    names = os.listdir(seg_dir)
    assert names, seg_dir
    for name in names:
        parsed = parse_segment_name(name)
        assert parsed is not None, (
            f"имя {name} не разбирается ротацией и индексацией (SPEC §20)")
        assert parsed[0] == 1
    # И заодно: шаблон, отправленный серверу, действительно указывал сюда.
    assert record_path_template(live["media_root"]).startswith(seg_dir)


def test_recording_is_remux_not_transcode(live):
    """SPEC §24 запрещает перекодирование архива. Сравнивается поток на
    входе с потоком в сегменте: кодек, размер кадра и частота обязаны
    совпасть. Юнит-тест это доказать не может — он видит только конфиг."""
    desired = {path_name(1): path_conf(live["source"],
                                       segment_duration_min=SEGMENT_MIN,
                                       media_root=live["media_root"])}
    sync_paths(live["client"], desired)
    seg_dir = segments_dir(live["media_root"])
    _wait(lambda: os.listdir(seg_dir), 30, "сегменты не появились")

    # Файл должен успеть набрать кадры, иначе ffprobe не увидит поток.
    path = os.path.join(seg_dir, sorted(os.listdir(seg_dir))[0])
    _wait(lambda: os.path.getsize(path) > 200_000, 30, "сегмент не растёт")

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,avg_frame_rate", "-of",
         "default=nw=1:nk=1", path], capture_output=True, text=True).stdout.split()

    assert probe[:3] == ["h264", "1280", "720"], probe
    assert probe[3] == "15/1", f"частота кадров изменилась: {probe[3]}"


def test_completed_segment_is_picked_up_for_the_archive(live):
    """`collect_complete_segments()` на файлах, которые пишет настоящий
    MediaMTX: пишущийся сегмент не должен попасть в архив (иначе в
    `video_segments` окажется строка на файл, который ещё растёт), а
    дописанный — должен."""
    desired = {path_name(1): path_conf(live["source"],
                                       segment_duration_min=SEGMENT_MIN,
                                       media_root=live["media_root"])}
    sync_paths(live["client"], desired)
    seg_dir = segments_dir(live["media_root"])
    _wait(lambda: os.listdir(seg_dir), 30, "сегменты не появились")

    # Пишущийся прямо сейчас файл — не готов ни при каком settle.
    assert collect_complete_segments(seg_dir, time.time(), SETTLE_SEC) == []

    # Тот же файл, если смотреть на него достаточно позже его последней
    # записи, — готов. Часы двигаются в аргументе, а не в системе: спать
    # минуту ради ротации в CI-джобе незачем.
    later = time.time() + SETTLE_SEC + 5
    got = collect_complete_segments(seg_dir, later, SETTLE_SEC)

    assert got, "дописанный сегмент не попал в архив"
    assert all(g["camera_id"] == 1 and g["size_bytes"] > 0 for g in got), got
