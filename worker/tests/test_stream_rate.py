"""§9 «Статус всех RTSP-потоков (онлайн/оффлайн, FPS, битрейт)».

Из трёх названных величин слой записи отдавал одну. Вместо битрейта
«Мониторинг» показывал столбец «Принято, МБ» — накопительный счётчик,
который у камеры, отвалившейся три часа назад, выглядит ровно так же
внушительно, как у исправной. FPS существовал только для камер аналитики
(`worker:fps` — темп детектора, а не потока), то есть для 2-3 камер из 120.

**Набор живёт здесь, а не рядом с worker.py, намеренно** — по той же
причине, что `test_record_alert_batch.py`: `worker.py` тянет
cv2/insightface/onnxruntime, и всё, что его импортирует, в лёгкой CI-джобе
уходит в skip. Поэтому вся арифметика и все решения вынесены в
`stream_rate.py` (только stdlib), а настоящий ffprobe по файлу настоящего
MediaMTX проверяет `test_stream_rate_live.py` в джобе `record-layer-live`.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stream_rate import (  # noqa: E402
    MIN_SAMPLE_INTERVAL_SEC, SegmentFpsCache, bitrate_kbps, fps_from_counts,
    parse_probe_output, probe_segment_fps, update_bitrates)


def state(cam_id, status="online", inbound_bytes=0):
    return {"camera_id": cam_id, "name": f"cam{cam_id}", "status": status,
            "inbound_bytes": inbound_bytes, "frames_in_error": 0,
            "online_since": None}


# --- битрейт ---------------------------------------------------------------

def test_bitrate_is_the_delta_of_the_counter_not_the_counter():
    """Два мегабайта за 10 секунд — это 1600 кбит/с, а не «2 МБ».

    Проверка ровно того, чего не было: столбец «Принято» показывал второе
    число, и по нему нельзя отличить камеру, которая пишется прямо сейчас,
    от камеры, которая писалась вчера.
    """
    assert bitrate_kbps((100.0, 0), 110.0, 2_000_000) == 1600.0


def test_first_sample_is_a_dash_not_a_zero():
    """До второй пробы величины нет — и это не «ноль килобит».

    Ноль в строке читается как «поток мёртв»; первый проход менеджера
    после старта воркера пометил бы так все 120 исправных камер.
    """
    assert bitrate_kbps(None, 100.0, 5_000) is None


def test_counter_reset_after_a_break_does_not_produce_a_negative_bitrate():
    """Слой записи пересоздаёт путь после обрыва, и счётчик начинается с нуля.

    Разность была бы отрицательной; «−12 Мбит/с» в интерфейсе хуже прочерка.
    """
    assert bitrate_kbps((100.0, 900_000), 110.0, 1_000) is None


def test_too_short_an_interval_is_refused():
    """Деление на десятые доли секунды превращает джиттер в «40 Мбит/с»."""
    assert bitrate_kbps((100.0, 0), 100.5, 100_000) is None


def test_zero_traffic_is_reported_as_zero_because_that_is_the_truth():
    """Путь заведён, байты не идут — ноль здесь честный ответ, не прочерк."""
    assert bitrate_kbps((100.0, 500_000), 110.0, 500_000) == 0.0


def test_short_ticks_keep_the_baseline_instead_of_moving_it():
    """Частые проходы не должны съедать базу отсчёта.

    Если проба обновляется на каждом вызове, интервал никогда не наберётся
    и битрейт не появится вовсе — отказ, который выглядел бы как «камера
    молчит», хотя молчит счётчик.
    """
    samples = {}
    states = {1: state(1, inbound_bytes=0)}
    rates, samples = update_bitrates(samples, states, 100.0)
    assert rates[1] is None and samples[1] == (100.0, 0)

    # Проход через 0.5 с — база остаётся прежней.
    states = {1: state(1, inbound_bytes=100_000)}
    rates, samples = update_bitrates(samples, states, 100.5)
    assert rates[1] is None
    assert samples[1] == (100.0, 0), "база отсчёта сдвинулась на коротком проходе"

    # Ещё через 9.5 с интервал набран, и считается он от ПЕРВОЙ пробы.
    states = {1: state(1, inbound_bytes=1_250_000)}
    rates, samples = update_bitrates(samples, states, 110.0)
    assert rates[1] == 1000.0


def test_unknown_state_does_not_poison_the_baseline():
    """Control API не ответил — `inbound_bytes` равен нулю по отсутствию данных.

    Записать этот ноль пробой значит после восстановления API получить
    скачок «весь счётчик за один проход», то есть выдуманный битрейт в
    сотни мегабит на каждой камере объекта.
    """
    samples = {1: (100.0, 1_000_000)}
    rates, samples = update_bitrates(samples, {1: state(1, status="unknown")}, 110.0)
    assert rates[1] is None
    assert samples[1] == (100.0, 1_000_000), "проба затёрта нулём из unknown"

    rates, samples = update_bitrates(samples, {1: state(1, inbound_bytes=1_100_000)}, 120.0)
    assert rates[1] == 40.0


def test_departed_cameras_do_not_leak_samples():
    """Выключенная камера не должна оставлять пробу навсегда."""
    samples = {1: (100.0, 10), 2: (100.0, 20)}
    _, samples = update_bitrates(samples, {1: state(1)}, 110.0)
    assert set(samples) == {1}


# --- FPS -------------------------------------------------------------------

def test_fps_is_measured_from_packets_not_declared_in_the_header():
    """250 пакетов за 10 секунд — 25 кадров в секунду."""
    assert fps_from_counts(250, 10.0) == 25.0
    assert fps_from_counts(70, 10.0) == 7.0


def test_a_sub_second_fragment_yields_no_fps():
    """Обрыв сразу после переворота даёт огрызок, на котором частное скачет."""
    assert fps_from_counts(3, 0.1) is None


def test_missing_probe_fields_yield_no_fps():
    assert fps_from_counts(None, 10.0) is None
    assert fps_from_counts(250, None) is None


def test_probe_output_is_parsed_by_field_name_not_by_position():
    """Дефект, который поймал только живой набор.

    Первая редакция просила `csv=p=0` и читала поля по позиции. ffprobe
    печатает их в СВОЁМ порядке: на настоящем сегменте MediaMTX выходит
    `4.133322,62` — сперва длительность. Разбор по позиции отказывал на
    каждом файле, а юнит-тест с выдуманной строкой «7500,300.0» был
    зелёным: он кодировал ту же ошибку, что и код.

    Поэтому здесь JSON, и поля намеренно записаны в «неудобном» порядке —
    длительность раньше пакетов, как их и печатает ffprobe.
    """
    out = '{"streams":[{"duration":"300.000000","nb_read_packets":"7500"}]}'
    assert parse_probe_output(out) == (7500, 300.0)
    assert fps_from_counts(*parse_probe_output(out)) == 25.0


def test_garbage_probe_output_does_not_raise():
    assert parse_probe_output("") == (None, None)
    assert parse_probe_output("не json вовсе") == (None, None)
    assert parse_probe_output('{"streams":[]}') == (None, None)
    assert parse_probe_output('{"streams":[{"duration":"N/A"}]}') == (None, None)


def test_a_failing_ffprobe_is_a_dash_not_a_crash():
    """FPS — украшение строки мониторинга.

    Проход менеджера в том же цикле синхронизирует пути записи и ставит
    статусы камер; уронить его из-за ffprobe нельзя (SPEC §2).
    """
    class Boom:
        def __call__(self, *a, **kw):
            raise OSError("ffprobe отсутствует")

    assert probe_segment_fps("/tmp/x.mp4", runner=Boom()) is None

    class Fails:
        returncode, stdout = 1, ""

    assert probe_segment_fps("/tmp/x.mp4", runner=lambda *a, **kw: Fails()) is None


# --- кэш проб --------------------------------------------------------------

class Probe:
    """Считает вызовы: смысл кэша в том, чтобы их не было лишних."""

    def __init__(self, fps=25.0):
        self.calls = []
        self.fps = fps

    def __call__(self, path):
        self.calls.append(path)
        return self.fps


def test_the_same_segment_is_probed_once_not_every_tick():
    """Проход менеджера идёт раз в 5 секунд, сегмент живёт 5 минут.

    Проба на каждом проходе означала бы 60 вызовов ffprobe на камеру там,
    где достаточно одного, — 7200 вызовов на объекте из 120 камер за время
    жизни одного сегмента.
    """
    probe = Probe()
    cache = SegmentFpsCache(probe=probe)
    newest = {1: (1000.0, "/media/cam1/a.mp4")}
    for tick in range(10):
        assert cache.refresh(newest, 1000.0 + tick, 900)[1] == 25.0
    assert probe.calls == ["/media/cam1/a.mp4"]


def test_a_new_segment_is_probed_again():
    probe = Probe()
    cache = SegmentFpsCache(probe=probe)
    cache.refresh({1: (1000.0, "/media/cam1/a.mp4")}, 1000.0, 900)
    cache.refresh({1: (1300.0, "/media/cam1/b.mp4")}, 1300.0, 900)
    assert probe.calls == ["/media/cam1/a.mp4", "/media/cam1/b.mp4"]


def test_fps_from_an_old_segment_is_not_shown_as_current():
    """Число, снятое с сегмента трёхчасовой давности, описывает прошлое.

    В строке рядом со статусом «оффлайн» оно читалось бы как «камера
    отдаёт 25 к/с» — то есть ровно та ложь, из-за которой столбец
    «Принято» и оказался бесполезен.
    """
    probe = Probe()
    cache = SegmentFpsCache(probe=probe)
    assert cache.refresh({1: (1000.0, "/media/cam1/a.mp4")}, 1000.0, 900)[1] == 25.0
    assert cache.refresh({1: (1000.0, "/media/cam1/a.mp4")}, 1000.0 + 10_000, 900)[1] is None
    assert probe.calls == ["/media/cam1/a.mp4"], "устаревший сегмент всё же пробовался"


def test_probe_budget_bounds_the_cost_of_one_tick():
    """Общий рестарт MediaMTX переворачивает все 120 сегментов разом.

    Без бюджета один проход стоил бы 120 × ~70 мс = 8.4 секунды — дольше
    самого прохода менеджера.
    """
    probe = Probe()
    cache = SegmentFpsCache(budget=8, probe=probe)
    newest = {cam: (1000.0, f"/media/cam{cam}/a.mp4") for cam in range(1, 121)}
    cache.refresh(newest, 1000.0, 900)
    assert len(probe.calls) == 8


def test_no_camera_starves_when_the_budget_is_short():
    """120 камер при бюджете 8 обходятся за 15 проходов ≈ 75 секунд.

    Это втрое меньше пятиминутного сегмента, то есть к следующему
    перевороту все значения свежие.
    """
    probe = Probe()
    cache = SegmentFpsCache(budget=8, probe=probe)
    newest = {cam: (1000.0, f"/media/cam{cam}/a.mp4") for cam in range(1, 121)}
    for _ in range(15):
        cache.refresh(newest, 1000.0, 900)
    assert len(set(probe.calls)) == 120, "часть камер не пробована ни разу"


def test_a_camera_whose_probe_fails_leaves_the_queue_anyway():
    """Иначе она пробуется на каждом проходе и вытесняет остальных.

    Свойство несущее: очередь обходится с начала, и продвижение держится
    ровно на том, что пробованная камера из неё уходит — в том числе когда
    ffprobe вернул `None`. Без этого камера с битым файлом заняла бы весь
    бюджет навсегда, и FPS не появился бы больше ни у одной.
    """
    probe = Probe(fps=None)
    cache = SegmentFpsCache(budget=1, probe=probe)
    newest = {1: (1000.0, "/media/cam1/битый.mp4"), 2: (1000.0, "/media/cam2/a.mp4")}
    assert cache.refresh(newest, 1000.0, 900)[1] is None
    assert cache.refresh(newest, 1000.0, 900)[2] is None
    assert probe.calls == ["/media/cam1/битый.mp4", "/media/cam2/a.mp4"]


def test_departed_cameras_do_not_leak_cache_entries():
    probe = Probe()
    cache = SegmentFpsCache(probe=probe)
    cache.refresh({1: (1000.0, "/a.mp4"), 2: (1000.0, "/b.mp4")}, 1000.0, 900)
    cache.refresh({1: (1000.0, "/a.mp4")}, 1000.0, 900)
    assert set(cache._done) == {1}


def test_a_camera_without_segments_has_no_fps():
    """Камера только что добавлена — первый сегмент ещё пишется."""
    cache = SegmentFpsCache(probe=Probe())
    assert cache.refresh({}, 1000.0, 900) == {}
