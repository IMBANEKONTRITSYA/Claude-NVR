"""FPS и битрейт §9 против **настоящего** MediaMTX и настоящего ffprobe.

Юнит-набор (`test_stream_rate.py`) проверяет арифметику и решения на
подставленных числах. Он по построению не может ответить на два вопроса,
от которых зависит, увидит ли дежурный на объекте правду:

* **растёт ли `bytesReceived` так, как предполагает расчёт битрейта.**
  Ровно на этом месте проект уже обжигался: до цикла 43 объём читался по
  `inboundBytes`, которого в закреплённой v1.16.0 нет, и «Принято» всегда
  показывало 0 МБ при любом трафике. Юнит-тесты были зелёными;
* **отдаёт ли ffprobe число кадров по файлу, который пишет MediaMTX.**
  Сегменты пишутся во fMP4 фрагментами; на таком файле счётчик кадров в
  заголовке может отсутствовать или описывать только первый фрагмент.

Оба вопроса решаются одним способом — запуском настоящего сервера и
настоящей записи. Набор опт-ин: нужен `MEDIAMTX_BIN` и `ffmpeg`; в CI их
готовит джоба `record-layer-live`, при локальном прогоне набор
пропускается.
"""
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from record_layer import MediaMTXClient, path_conf, path_name, segments_dir
from record_status import path_live, stream_states
from stream_rate import SegmentFpsCache, probe_segment_fps, update_bitrates

MEDIAMTX_BIN = os.environ.get("MEDIAMTX_BIN")

pytestmark = pytest.mark.skipif(
    not MEDIAMTX_BIN or not os.path.exists(MEDIAMTX_BIN)
    or not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="нужен настоящий MediaMTX (MEDIAMTX_BIN), ffmpeg и ffprobe — см. "
           "джобу record-layer-live в .github/workflows/ci.yml",
)

CAMS = [(1, "Проходная")]

# Поток публикуется с этой частотой, и её же обязан показать FPS.
SOURCE_FPS = 15


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
    # Сегмент в минуту — минимум, который принимает path_conf; файл
    # переворачивается по границе, и ждать его целиком не нужно: тест
    # завершает публикацию и MediaMTX дописывает текущий сегмент.
    client.add_path(path_name(1), path_conf(source, segment_duration_min=1,
                                            media_root=str(media_root)))
    camera = None
    try:
        camera = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-re",
             "-f", "lavfi", "-i", f"testsrc2=size=640x360:rate={SOURCE_FPS}",
             "-c:v", "libx264", "-preset", "ultrafast", "-g", str(SOURCE_FPS),
             "-pix_fmt", "yuv420p", "-f", "rtsp", "-rtsp_transport", "tcp",
             source],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _wait(lambda: path_live(client.runtime_paths().get(path_name(1))), 40,
              "запись не пошла")
        yield {"client": client, "camera": camera, "media_root": Path(media_root)}
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


def test_bitrate_is_measured_from_the_real_byte_counter(live):
    """Битрейт живого потока — положительный и правдоподобный.

    Верхняя граница проверяется не для красоты: если поле счётчика вдруг
    поедет (как уже было с `inboundBytes`), а расчёт останется, ошибка
    вылезет именно неправдоподобным числом, а не нулём. 640×360 у
    ultrafast x264 — это единицы мегабит, но никак не сотни.
    """
    client = live["client"]
    _wait(lambda: stream_states(CAMS, client.runtime_paths())[1]["inbound_bytes"] > 0,
          20, "счётчик байтов не тронулся")

    samples = {}
    _, samples = update_bitrates(samples, stream_states(CAMS, client.runtime_paths()),
                                 time.time())
    # Интервал заведомо больше MIN_SAMPLE_INTERVAL_SEC.
    time.sleep(4)
    rates, _ = update_bitrates(samples, stream_states(CAMS, client.runtime_paths()),
                              time.time())

    assert rates[1] is not None, "битрейт не посчитался на живом потоке"
    assert 10 < rates[1] < 100_000, (
        f"битрейт {rates[1]} кбит/с неправдоподобен — проверьте, из какого "
        f"поля /v3/paths/list берётся объём принятого")


def test_fps_is_probed_from_a_real_mediamtx_segment(live):
    """FPS снимается с файла, который записал сам MediaMTX.

    Публикуется 15 к/с — столько и обязано получиться. Допуск в один кадр:
    сегмент режется по границе, и на краях в него попадает неполная
    секунда.
    """
    root = live["media_root"]
    files = sorted(Path(segments_dir(str(root))).rglob("*.mp4"))
    assert files, (
        "MediaMTX не записал ни одного сегмента — проверять FPS не на чем")

    # Берётся первый файл: последний может быть ещё не дописан.
    fps = probe_segment_fps(str(files[0]))
    assert fps is not None, (
        f"ffprobe не отдал число кадров по fMP4 от MediaMTX ({files[0]})")
    assert abs(fps - SOURCE_FPS) <= 1.0, (
        f"поток публикуется на {SOURCE_FPS} к/с, а измерено {fps}")


def test_the_cache_probes_a_real_file_once(live):
    """Кэш на настоящем файле ведёт себя так же, как на подставленном.

    Проверяется здесь, а не только в юнит-наборе, потому что цена ошибки
    видна лишь на настоящем ffprobe: 60 вызовов на камеру за время жизни
    одного сегмента вместо одного.
    """
    root = live["media_root"]
    files = sorted(Path(segments_dir(str(root))).rglob("*.mp4"))
    assert files

    calls = []

    def counting(path):
        calls.append(path)
        return probe_segment_fps(path)

    cache = SegmentFpsCache(probe=counting)
    newest = {1: (time.time(), str(files[0]))}
    first = cache.refresh(newest, time.time(), 900)[1]
    for _ in range(5):
        assert cache.refresh(newest, time.time(), 900)[1] == first
    assert len(calls) == 1, f"файл пробован {len(calls)} раз вместо одного"


def test_a_dead_stream_reads_zero_and_never_a_negative_number(live):
    """Что на самом деле делает MediaMTX с счётчиком на обрыве.

    Замерено здесь, а не взято из документации: когда источник пути
    отваливается, `bytesReceived` **сбрасывается в ноль**. Первая
    редакция этого теста исходила из обратного («счётчик остаётся
    большим») и на нём же и упала — ровно тот случай, ради которого живой
    набор и заведён.

    Следствие для расчёта: пара проб, разложенная по обе стороны сброса,
    даёт отрицательную разность. Сторож `now_bytes < prev_bytes` отдаёт на
    ней прочерк — «−12 Мбит/с» в строке камеры было бы хуже пустоты, — а
    следующая пара, уже целиком после сброса, даёт честный ноль.
    """
    client = live["client"]
    before = stream_states(CAMS, client.runtime_paths())
    assert before[1]["inbound_bytes"] > 0, "на живом потоке счётчик не рос"

    live["camera"].terminate()
    live["camera"].wait(timeout=10)
    _wait(lambda: not path_live(client.runtime_paths().get(path_name(1))), 30,
          "MediaMTX не отметил обрыв")

    after = stream_states(CAMS, client.runtime_paths())
    assert after[1]["inbound_bytes"] == 0, (
        "апстрим перестал сбрасывать счётчик на обрыве — это хорошая "
        "новость, но комментарий про сброс в stream_rate.py надо "
        "переписать по факту")

    # Проба до сброса против пробы после него — разность отрицательна.
    samples = {1: (time.time() - 10, before[1]["inbound_bytes"])}
    rates, samples = update_bitrates(samples, after, time.time())
    assert rates[1] is None, (
        f"на сбросе счётчика посчитан битрейт {rates[1]} вместо прочерка")

    # Обе пробы уже после сброса — честный ноль.
    time.sleep(4)
    rates, _ = update_bitrates(samples, stream_states(CAMS, client.runtime_paths()),
                               time.time())
    assert rates[1] == 0.0, (
        f"на мёртвом потоке битрейт {rates[1]}, а должен быть ноль")
