"""Скорость потоков слоя записи: битрейт и FPS (SPEC §9).

SPEC §9 перечисляет содержимое статуса потоков поимённо: «Статус всех
RTSP-потоков (**онлайн/оффлайн, FPS, битрейт**)». До этого модуля из трёх
названных величин слой записи отдавал одну. `record_status.stream_states()`
публиковал `status`, `inbound_bytes`, `frames_in_error` и `online_since`, а
страница «Мониторинг» показывала «Принято, МБ» — **накопительный счётчик**,
а не скорость. Разница не косметическая: камера, отдающая 200 кбит/с вместо
положенных четырёх мегабит (сбитый профиль, забитый канал, деградация), по
счётчику неотличима от исправной — её число просто растёт медленнее, а
скорость роста в интерфейсе никто не измеряет глазами. Именно этот случай
§9 и просит показать, и виден он только в кбит/с.

FPS при этом существовал только для камер аналитики (`worker:fps` — темп
обработки кадров детектором, а не темп потока), то есть для 2-3 камер из
120. На остальных 118 обе величины §9 отсутствовали.

**Ни FPS, ни битрейта в Control API MediaMTX нет.** Проверено запуском
закреплённого бинарника v1.16.0 (`docker-compose.yml`): `/v3/paths/list`
отдаёт по пути `name`, `confName`, `ready`/`available` с их временами,
`online`, `source`, `tracks`, `bytesReceived`, `bytesSent`, `readers` — и
ничего похожего на скорость. Отсюда два разных источника:

* **битрейт** считается здесь из разности `bytesReceived` между проходами
  менеджера. Это точная величина, а не оценка: счётчик ведёт сам сервер,
  дельта делится на фактически прошедшее время. Ни декодирования, ни
  обращения к диску;
* **FPS** брать неоткуда, кроме самих записанных файлов, поэтому он
  снимается `ffprobe` с последнего **дописанного** сегмента камеры.

Почему FPS считается по числу пакетов, а не по `avg_frame_rate`: первое —
измеренная величина (`nb_read_packets` / фактическая длительность), второе
— то, что объявлено в заголовке. На двух проверенных потоках (25 и 7 к/с,
настоящий MediaMTX, настоящая запись) они совпали, но совпадать обязаны не
всегда: смысл показателя на «Мониторинге» именно в том, чтобы поймать
камеру, которая объявляет 25 к/с и отдаёт 4. Цена различения — 13 мс:
замер на пятиминутном фрагменте 1280×720 (медиана из пяти прогонов) даёт
69.7 мс против 56.7 мс, и оба варианта читают индекс, а не декодируют.

Модуль намеренно не тянет ни cv2, ни БД, ни Redis — только stdlib. Причина
та же, что у `record_status.py`: тесты этих решений обязаны выполняться в
лёгкой джобе CI, которая не ставит OpenCV. Набор, живущий в `worker.py`,
уходил бы там в skip целиком — ровно тот дефект, из-за которого алерты §9
цикла 50 пришлось выносить в отдельный модуль.
"""
from __future__ import annotations

import json
import shutil
import subprocess

# Ниже этого интервала дельту счётчика не считаем: проход менеджера идёт
# раз в ~5 секунд, но при отставании цикла два вызова могут прийти почти
# подряд, и деление на десятые доли секунды превращает джиттер сети в
# «битрейт 40 Мбит/с». Предыдущая проба при этом НЕ заменяется — иначе на
# частых проходах база отсчёта сдвигалась бы вечно и битрейт не появился
# бы никогда.
MIN_SAMPLE_INTERVAL_SEC = 2.0

# Сколько камер опрашивать ffprobe за один проход менеджера. Проба стоит
# ~70 мс; без ограничения объект из 120 камер, у которых сегменты
# перевернулись одновременно (общий рестарт MediaMTX), дал бы 8.4 секунды
# в одном проходе — дольше самого прохода. При бюджете 8 полный обход 120
# камер занимает 15 проходов ≈ 75 секунд, что втрое меньше пятиминутного
# сегмента: к следующему перевороту все значения свежие.
FPS_PROBE_BUDGET = 8

# ffprobe читает индекс локального файла; секунды здесь — защита от
# зависшего вызова на сетевом хранилище, а не рабочий режим.
FPS_PROBE_TIMEOUT_SEC = 15


def bitrate_kbps(previous: tuple[float, int] | None,
                 now_ts: float, now_bytes: int) -> float | None:
    """Кбит/с по двум пробам счётчика `bytesReceived`.

    `previous` — пара «время пробы, значение счётчика» с прошлого прохода.

    `None` возвращается там, где числа нет, и это **не** ноль:

    * первая проба по камере — база отсчёта ещё не с чем сравнивать;
    * счётчик уменьшился. Так выглядит обрыв: **MediaMTX сбрасывает
      `bytesReceived` в ноль**, когда источник пути отваливается —
      замерено на v1.16.0 в `test_stream_rate_live.py`, а не взято из
      документации. Разность на такой паре отрицательна, а «−12 Мбит/с» в
      строке камеры хуже прочерка; следующая пара, уже целиком после
      сброса, даёт честный ноль;
    * прошло меньше `MIN_SAMPLE_INTERVAL_SEC`.

    Ноль — тоже ответ, и честный: путь есть, байты не идут.
    """
    if previous is None:
        return None
    prev_ts, prev_bytes = previous
    dt = now_ts - prev_ts
    if dt < MIN_SAMPLE_INTERVAL_SEC:
        return None
    if now_bytes < prev_bytes:
        return None
    return round((now_bytes - prev_bytes) * 8 / 1000.0 / dt, 1)


def update_bitrates(previous: dict[int, tuple[float, int]],
                    states: dict[int, dict],
                    now_ts: float,
                    unknown_status: str = "unknown",
                    ) -> tuple[dict[int, float | None], dict[int, tuple[float, int]]]:
    """Битрейты по всем камерам и новая карта проб.

    Возвращает `(битрейты, пробы)`; вызывающий держит вторую между
    проходами.

    Камеры в состоянии `unknown` пропускаются целиком: Control API не
    ответил, `inbound_bytes` у них равен нулю **по отсутствию данных**, и
    записать этот ноль пробой значило бы после восстановления API получить
    гигантский скачок битрейта на ровном месте. Их прошлая проба остаётся
    нетронутой, а битрейт — прочерком.
    """
    rates: dict[int, float | None] = {}
    samples = dict(previous)
    for cam_id, st in states.items():
        if st.get("status") == unknown_status:
            rates[cam_id] = None
            continue
        now_bytes = int(st.get("inbound_bytes") or 0)
        prev = previous.get(cam_id)
        rates[cam_id] = bitrate_kbps(prev, now_ts, now_bytes)
        # Проба обновляется только тогда, когда по ней уже посчитано (или
        # считать было нечем вовсе). Иначе на слишком частых проходах база
        # сдвигалась бы каждый раз и интервал никогда не набирался.
        if prev is None or now_ts - prev[0] >= MIN_SAMPLE_INTERVAL_SEC:
            samples[cam_id] = (now_ts, now_bytes)
    # Камеры, выбывшие из слоя записи, пробу за собой не оставляют.
    for cam_id in list(samples):
        if cam_id not in states:
            del samples[cam_id]
    return rates, samples


def parse_probe_output(stdout: str) -> tuple[int | None, float | None]:
    """Разбирает `nb_read_packets` и `duration` из вывода ffprobe.

    **Формат — JSON, и это не вкус.** Первая редакция просила `csv=p=0` и
    читала поля по позиции, полагая, что ffprobe печатает их в порядке
    `-show_entries`. Он печатает в своём: на настоящем сегменте MediaMTX
    строка выглядит как `4.133322,62` — сперва длительность, потом пакеты.
    По позиции разбор давал `int("4.133322")` → отказ, то есть FPS не
    появлялся **ни на одном** файле, а юнит-тест с выдуманной строкой
    «7500,300.0» был при этом зелёным. Поймал только живой набор.

    С именами полей порядок значения не имеет вовсе.
    """
    try:
        streams = json.loads(stdout or "").get("streams") or []
    except (ValueError, AttributeError):
        return None, None
    if not streams:
        return None, None
    stream = streams[0]
    try:
        packets = int(stream.get("nb_read_packets"))
    except (TypeError, ValueError):
        packets = None
    try:
        duration = float(stream.get("duration"))
    except (TypeError, ValueError):
        duration = None
    return packets, duration


def fps_from_counts(packets: int | None, duration: float | None) -> float | None:
    """Кадры в секунду по числу пакетов и длительности фрагмента.

    Пакет видеодорожки — кадр: сегменты слоя записи пишутся remux'ом без
    перекодирования (SPEC §19), дорожка одна, и делить её пакеты на
    длительность корректно.

    Слишком короткий фрагмент отбрасывается: у сегмента в доли секунды
    (обрыв сразу после переворота файла) частное скачет как угодно.
    """
    if not packets or not duration or duration < 1.0:
        return None
    return round(packets / duration, 1)


def probe_segment_fps(path: str, *, runner=None,
                      timeout: float = FPS_PROBE_TIMEOUT_SEC) -> float | None:
    """FPS записанного сегмента через ffprobe. `None`, если снять нечем.

    `runner` подменяется в тестах: сам разбор и решения проверяются без
    ffmpeg на машине, а живой набор `test_stream_rate_live.py` гоняет
    настоящий ffprobe по файлу, записанному настоящим MediaMTX.

    Никогда не бросает: FPS — украшение строки мониторинга, и упавший
    ffprobe не должен ронять проход менеджера, который в том же цикле
    синхронизирует пути записи.
    """
    run = runner or subprocess.run
    if runner is None and not shutil.which("ffprobe"):
        return None
    try:
        proc = run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-count_packets", "-show_entries", "stream=nb_read_packets,duration",
             "-of", "json", path],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    if getattr(proc, "returncode", 1) != 0:
        return None
    return fps_from_counts(*parse_probe_output(getattr(proc, "stdout", "") or ""))


class SegmentFpsCache:
    """FPS по камерам с пробой каждого сегмента ровно один раз.

    Сегмент — файл на 5-10 минут (SPEC §20), поэтому проба привязана к
    **пути файла**, а не ко времени: пока камера пишет тот же сегмент,
    ffprobe по ней не вызывается вовсе. Новый файл — одна проба.

    Очередь на пробу обходится с начала, и этого достаточно: камера
    покидает её сразу после пробы — результат кладётся в `_done` **и когда
    он `None`** (ffprobe не смог), иначе неудачная проба возвращала бы
    камеру в очередь вечно и вытесняла оттуда остальные. За счёт этого
    очередь на каждом проходе короче предыдущей, и 120 камер обходятся за
    15 проходов даже при бюджете 8.
    """

    def __init__(self, budget: int = FPS_PROBE_BUDGET, probe=probe_segment_fps):
        self._probe = probe
        self._budget = max(1, int(budget))
        self._done: dict[int, tuple[str, float | None]] = {}

    def refresh(self, newest: dict[int, tuple[float, str]],
                now_ts: float, stale_after_sec: float) -> dict[int, float | None]:
        """FPS по камерам; `newest` — «камера → (время конца, путь файла)».

        Значение **устаревает**: FPS, снятый с сегмента трёхчасовой
        давности, описывает не поток, а его прошлое, и в строке рядом со
        статусом «оффлайн» читался бы как «камера отдаёт 25 к/с». За
        порогом свежести отдаётся прочерк, и файл не пробуется.
        """
        pending: list[int] = []
        out: dict[int, float | None] = {}
        for cam_id, (ended_ts, path) in newest.items():
            if now_ts - ended_ts > stale_after_sec:
                out[cam_id] = None
                continue
            cached = self._done.get(cam_id)
            if cached and cached[0] == path:
                out[cam_id] = cached[1]
            else:
                pending.append(cam_id)
                out[cam_id] = cached[1] if cached else None

        for cam_id in sorted(pending)[:self._budget]:
            _, path = newest[cam_id]
            fps = self._probe(path)
            # Кладётся и `None`: см. шапку класса — иначе камера, на
            # которой ffprobe не смог, остаётся в очереди навсегда.
            self._done[cam_id] = (path, fps)
            out[cam_id] = fps

        for cam_id in list(self._done):
            if cam_id not in newest:
                del self._done[cam_id]
        return out
