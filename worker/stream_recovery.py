"""Активное восстановление потоков слоя записи (SPEC §19, §13, §2).

**Зачем модуль появился.** §19 требует «восстановление потока ≤ 5 секунд
после обрыва». Цикл 40 впервые это измерил (`perf/bench_recovery.py`) и
получил **6.6 с в худшем случае** на настоящем MediaMTX. Причина не в
нагрузке и не в железе: MediaMTX повторяет подключение к статическому
источнику раз в 5 секунд, паузу эту в конфигурации задать нельзя, и она
одна съедает весь бюджет §19. Числа совпали до сотых на двух несравнимых
процессорах (песочница Xeon @2.80GHz и раннер EPYC 7763) — так ведёт себя
константа, а не производительность.

Вывод цикла 40 был верен: **бюджет §19 недостижим одной логикой
медиасервера**. Отсюда этот модуль — воркер замечает возвращение камеры
раньше, чем до неё дойдёт очередь у MediaMTX, и заставляет сервер
подключиться немедленно.

**Что уже пробовали и почему это не сработало** (цикл 40, 6.3):
`/v3/config/paths/replace` той же самой конфигурацией — MediaMTX не
перезапускает обработчик источника, если конфигурация не изменилась:
19 «пинков» за обрыв, восстановление те же 6.54 с. Работает только
`delete` + `add`: путь пересоздаётся, и подключение начинается сразу.
Именно поэтому цикл 40 отложил вариант — «рвать конфигурацию каждой
отвалившейся камеры несколько раз в секунду» на 120 путях неприемлемо.

**Решение — не пинать вслепую, а пинать по факту.** Прежде чем трогать
конфигурацию, воркер спрашивает саму камеру, отдаёт ли она поток
(`rtsp_alive()`), и пересоздаёт путь **один раз** — в тот момент, когда
камера ответила. На обрыв приходится один `delete`+`add`, а не десяток:
пока камера молчит, пинков нет вовсе.

**Экспоненциальная задержка (§2, §13) — здесь и по-настоящему.** Цикл 40
попутно выяснил, что обещанной в §2 и §13 «экспоненциальной задержки» у
слоя записи нет вообще: обрыв на 30 секунд восстанавливался ровно так же,
как на 5. Теперь она есть, и на двух разных величинах, потому что цена у
них разная:

* **опрос камеры** (`rtsp_alive`) — дешёвый и без побочных эффектов, но
  именно он определяет, насколько быстро мы заметим возвращение. Растёт
  0.25 → 0.5 → 1 → 2 с и упирается в потолок `probe_max`. Потолок здесь
  не «сколько не жалко», а прямое следствие §19: к паузе опроса
  добавляется ~1.5 с на подключение и открытие файла, и всё вместе обязано
  уложиться в 5 с;
* **пересоздание пути** (`delete`+`add`) — с побочным эффектом: закрывает
  текущий файл записи и дёргает конфигурацию сервера. Растёт 5 → 10 → 20
  → 40 → 60 с. Смысл: если камера отвечает на RTSP, а запись всё равно не
  идёт (неверный путь в URL, сменившийся пароль, кодек, который MediaMTX
  не принимает), то повторять пересоздание раз в секунду бессмысленно и
  вредно — отказ не в тайминге.

То есть §13 выполняется там, где он защищает (повторные попытки по
сломанной камере затухают), и не мешает там, где §19 требует скорости.

**Модуль намеренно на одном stdlib** — как `record_layer.py`,
`onvif_client.py`, `backoff.py`. CI-джоба воркера не ставит
cv2/insightface, поэтому вся логика отсюда проверяется в CI: чистая часть
— против двойника Control API (`tests/test_stream_recovery.py`), сетевая
— против настоящего MediaMTX (`tests/test_stream_recovery_live.py`,
джоба `record-layer-live`).
"""
from __future__ import annotations

import logging
import socket
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from record_layer import MediaMTXError, camera_id_from_path
from record_status import path_live

logger = logging.getLogger("facewatch.worker")

DEFAULT_RTSP_PORT = 554

# Опрос камеры: с чего начинается пауза и во что упирается.
PROBE_BASE_SEC = 0.25
PROBE_MAX_SEC = 2.0

# Пересоздание пути: первая пауза и потолок.
KICK_BASE_SEC = 5.0
KICK_MAX_SEC = 60.0

# Сколько ждать ответа камеры. Верхняя граница осмысленности: камера,
# которой нужно больше двух секунд на строку состояния, всё равно не
# уложится в бюджет §19, а нить опроса занимать будет.
PROBE_TIMEOUT_SEC = 2.0

# Сколько камер опрашивать одновременно. На объекте, где моргнул
# коммутатор, отвалиться могут все сразу, а недоступный хост держит
# соединение до таймаута; без пула проход опроса растянулся бы на
# `число камер × PROBE_TIMEOUT_SEC`.
PROBE_WORKERS = 16

# Период прохода супервизора. Влияет на то, как быстро мы заметим сам
# обрыв (то есть когда начнём опрашивать камеру), но не на время
# восстановления: к моменту возвращения камеры опрос уже идёт по своему
# расписанию.
TICK_SEC = 1.0

# Коды RTSP, по которым камера считается живой.
#
# 200 — отдала описание потока. 401/403 — потребовала учётные данные, то
# есть RTSP-сервер камеры поднят и обслуживает запросы; этого достаточно,
# потому что учётка есть у MediaMTX, а не у нас (см. ниже). Всё остальное
# — не живая: 404 отдаёт и MediaMTX на путь без публикатора, и камера, у
# которой запрошенный профиль ещё не поднялся.
ALIVE_STATUSES = frozenset({200, 401, 403})


def _rtsp_target(url: str) -> tuple[str, int, str]:
    """(host, port, запрос без учётных данных) из RTSP-URL камеры.

    **Пароль в запрос не попадает, и это не мелочь.** Опрос ходит на
    камеру каждые пару секунд, пока она в обрыве; посылать в него
    `Authorization` (тем более Basic) значило бы раскладывать учётку слоя
    записи по сети на каждом моргании коммутатора — при том, что ответ
    401 нас устраивает не меньше, чем 200: он означает ровно то, что нам
    нужно знать — камера отвечает. Учётные данные остаются там, где им и
    место: в конфигурации пути MediaMTX.
    """
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    port = parts.port or DEFAULT_RTSP_PORT
    netloc = f"{host}:{port}"
    target = urllib.parse.urlunsplit((parts.scheme or "rtsp", netloc,
                                      parts.path or "/", parts.query, ""))
    return host, port, target


def rtsp_alive(url: str, timeout: float = PROBE_TIMEOUT_SEC) -> bool:
    """Отдаёт ли камера поток прямо сейчас — один DESCRIBE без учётки.

    Почему DESCRIBE, а не TCP-connect: на объекте между воркером и камерой
    может стоять коммутатор, который принимает соединение и на мёртвом
    порту, а в песочнице и в CI «камера» — это путь MediaMTX, чей
    RTSP-порт слушает всегда. TCP-проба сказала бы «жива» в обоих
    случаях. DESCRIBE отвечает на нужный вопрос: **есть ли поток**
    (проверено на MediaMTX v1.16.0 — путь без публикатора отвечает
    `404 Not Found`, с публикатором `200 OK`).

    Исключений не бросает: недоступная камера — штатное состояние, ради
    которого модуль и написан, а не ошибка.
    """
    try:
        host, port, target = _rtsp_target(url)
        if not host:
            return False
        request = (f"DESCRIBE {target} RTSP/1.0\r\n"
                   "CSeq: 1\r\n"
                   "Accept: application/sdp\r\n"
                   "User-Agent: FaceWatch\r\n\r\n")
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(request.encode("ascii", "ignore"))
            # Нужна только строка состояния. Читается до первого CRLF, а не
            # весь ответ: SDP камеры бывает килобайтами, и ждать его конца
            # значило бы держать соединение дольше, чем нужно для ответа.
            buf = b""
            while b"\r\n" not in buf and len(buf) < 256:
                chunk = sock.recv(128)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return False
    line = buf.split(b"\r\n", 1)[0].decode("ascii", "replace")
    parts = line.split()
    if len(parts) < 2 or not parts[0].upper().startswith("RTSP/"):
        return False
    try:
        return int(parts[1]) in ALIVE_STATUSES
    except ValueError:
        return False


def down_paths(desired: dict[str, dict], runtime: dict[str, dict] | None) -> dict[str, str]:
    """Пути слоя записи, по которым сейчас не идёт поток: имя → RTSP-URL.

    `runtime is None` — Control API не ответил. Тогда результат пустой, а
    не «все камеры в обрыве»: причина ровно та же, по которой
    `record_status.stream_states()` в этом случае отвечает `unknown` —
    неизвестно ≠ потеряно, и пересоздавать 120 путей на каждом рестарте
    медиасервера нельзя.

    Отсутствие пути в рантайме — это обрыв (путь заведён в конфигурации,
    но обработчика нет), и он тоже лечится пересозданием.

    «Идёт ли поток» решает `record_status.path_live()`, а не поле `online`:
    у пути со статическим источником — а других слой записи не заводит —
    `online` не падает никогда, и супервизор, спрашивающий его, не увидел
    бы ни одного обрыва вовсе (см. шапку `record_status.py`).
    """
    if runtime is None:
        return {}
    out: dict[str, str] = {}
    for name, conf in desired.items():
        source = conf.get("source")
        if not isinstance(source, str) or not source.startswith("rtsp"):
            # Путь не с RTSP-источником (ручная публикация при диагностике)
            # — опрашивать нечего, пересоздавать не наше дело.
            continue
        if not path_live(runtime.get(name)):
            out[name] = source
    return out


class RecoveryPlanner:
    """Кто и когда опрашивается, и когда пересоздаётся путь.

    Чистая логика без сети и без времени в аргументах конструктора: время
    приходит параметром, поэтому расписание проверяется тестом за
    миллисекунды, а не за минуты ожидания.
    """

    def __init__(self, *, probe_base: float = PROBE_BASE_SEC,
                 probe_max: float = PROBE_MAX_SEC,
                 kick_base: float = KICK_BASE_SEC,
                 kick_max: float = KICK_MAX_SEC):
        self.probe_base = probe_base
        self.probe_max = probe_max
        self.kick_base = kick_base
        self.kick_max = kick_max
        # имя пути → состояние обрыва
        self._state: dict[str, dict] = {}

    @staticmethod
    def _backoff(base: float, cap: float, attempts: int) -> float:
        return min(cap, base * (2 ** max(0, attempts)))

    def forget(self, name: str) -> None:
        """Путь вернулся в онлайн (или выбыл из слоя записи) — состояние
        обрыва снимается целиком, вместе с накопленной задержкой.

        Именно целиком: следующий обрыв этой камеры обязан начаться с
        быстрого опроса. Оставлять накопленную задержку значило бы, что
        камера, которую один раз чинили полчаса, потом восстанавливается
        после каждого моргания минуту — то есть §19 нарушался бы для неё
        навсегда.
        """
        self._state.pop(name, None)

    def sync(self, down: dict[str, str]) -> None:
        """Снимает состояние с путей, которых больше нет в списке обрыва."""
        for name in [n for n in self._state if n not in down]:
            self.forget(name)

    def due_probes(self, down: dict[str, str], now: float) -> list[tuple[str, str]]:
        """Кого опрашивать на этом проходе: [(имя пути, RTSP-URL)].

        Первый проход после обрыва опрашивает сразу — задержка растёт
        только на неудачах.
        """
        out = []
        for name, url in sorted(down.items()):
            st = self._state.get(name)
            if st is None:
                st = {"down_since": now, "probe_attempts": 0, "next_probe_at": now,
                      "kicks": 0, "next_kick_at": now, "last_alive": None}
                self._state[name] = st
            if now >= st["next_probe_at"]:
                out.append((name, url))
        return out

    def probed(self, name: str, alive: bool, now: float) -> bool:
        """Учитывает результат опроса. True — пора пересоздавать путь.

        Пересоздание разрешается только когда камера **ответила**: пинок
        по молчащей камере ничего не чинит, а конфигурацию рвёт.
        """
        st = self._state.get(name)
        if st is None:
            return False
        st["last_alive"] = alive
        if not alive:
            st["probe_attempts"] += 1
            st["next_probe_at"] = now + self._backoff(
                self.probe_base, self.probe_max, st["probe_attempts"] - 1)
            return False
        # Камера ответила: следующий опрос — с базовой паузой. Если
        # пересоздание не поможет, мы вернёмся сюда через неё и увидим
        # ситуацию «отвечает, но не пишется» — её лечит задержка пинков.
        st["probe_attempts"] = 0
        st["next_probe_at"] = now + self.probe_base
        return now >= st["next_kick_at"]

    def kicked(self, name: str, now: float) -> None:
        st = self._state.get(name)
        if st is None:
            return
        st["kicks"] += 1
        st["next_kick_at"] = now + self._backoff(
            self.kick_base, self.kick_max, st["kicks"] - 1)

    def snapshot(self, now: float) -> dict:
        """Состояние для интерфейса и логов: по какой камере сколько ждём
        и сколько раз пересоздавали путь."""
        out = {}
        for name, st in self._state.items():
            cam_id = camera_id_from_path(name)
            if cam_id is None:
                continue
            out[cam_id] = {
                "down_for_sec": round(max(0.0, now - st["down_since"]), 1),
                "probes_failed": st["probe_attempts"],
                "kicks": st["kicks"],
                "camera_answering": st["last_alive"],
            }
        return out


def kick_path(client, name: str, conf: dict) -> None:
    """Пересоздание пути: `delete` + `add` (см. шапку — `replace` не
    перезапускает источник).

    Порядок обязателен именно такой, и между вызовами есть щель, в которую
    путь не существует. Это осознанно: `add` на существующий путь MediaMTX
    отвергает, а «щель» стоит доли миллисекунды на камере, которая и так в
    обрыве, — терять в ней нечего. Если `add` всё же не пройдёт,
    `sync_paths()` менеджера заведёт путь заново на ближайшем проходе
    (≤ 10 с), поэтому камера не остаётся без записи насовсем.
    """
    try:
        client.delete_path(name)
    except MediaMTXError:
        # Путь мог исчезнуть сам (менеджер пересобрал конфигурацию, камеру
        # выключили) — тогда остаётся просто завести его заново.
        logger.debug("пинок: путь не удалось удалить", exc_info=True,
                     extra={"path": name})
    client.add_path(name, conf)


def recover_once(client, desired: dict[str, dict], runtime: dict[str, dict] | None,
                 planner: RecoveryPlanner, now: float, *,
                 probe=rtsp_alive, executor: ThreadPoolExecutor | None = None) -> dict:
    """Один проход супервизора. Возвращает статистику прохода.

    Разделение «планировщик решает — проход делает» держит всю сетевую
    часть в одном месте и позволяет проверить решения (кого опрашивать,
    когда пинать) без сети вовсе.
    """
    down = down_paths(desired, runtime)
    planner.sync(down)
    stats = {"down": len(down), "probed": 0, "alive": 0, "kicked": 0, "failed": 0}
    if not down:
        return stats

    targets = planner.due_probes(down, now)
    if not targets:
        return stats
    stats["probed"] = len(targets)

    if executor is not None and len(targets) > 1:
        results = list(executor.map(lambda t: probe(t[1]), targets))
    else:
        results = [probe(url) for _, url in targets]

    for (name, _url), alive in zip(targets, results):
        if alive:
            stats["alive"] += 1
        # Время берётся заново: опрос недоступной камеры длится до
        # таймаута, и планировать следующий проход от `now`, снятого до
        # опроса, значило бы назначать его в прошлое.
        if not planner.probed(name, alive, time.monotonic()):
            continue
        try:
            kick_path(client, name, desired[name])
            planner.kicked(name, time.monotonic())
            stats["kicked"] += 1
            logger.info(
                "поток записи: камера ответила, пересоздаю путь",
                extra={"path": name, "camera_id": camera_id_from_path(name),
                       "event": "record_stream_kick"})
        except MediaMTXError:
            stats["failed"] += 1
            planner.kicked(name, time.monotonic())
            logger.error("не удалось пересоздать путь записи", exc_info=True,
                         extra={"path": name})
    return stats


class RecoverySupervisor:
    """Нить супервизора: свой период, независимый от прохода менеджера.

    Почему отдельная нить, а не шаг менеджера: проход менеджера — ~10 с
    (опрос БД, синхронизация путей, индексация сегментов, retention), и
    втискивать в него секундный опрос значило бы либо ускорить всё
    остальное на порядок, либо потерять §19. Нить останавливается по
    общему `stop_event` и **джойнится** (`stop()`), а не бросается
    демоном: между `delete` и `add` она держит путь несуществующим, и
    оборвать её в этой точке — единственный способ оставить камеру без
    записи до следующего прохода менеджера.
    """

    def __init__(self, client_factory, desired_provider, *, interval: float = TICK_SEC,
                 planner: RecoveryPlanner | None = None, probe=rtsp_alive,
                 workers: int = PROBE_WORKERS):
        self._client_factory = client_factory
        self._desired_provider = desired_provider
        self.interval = interval
        self.planner = planner or RecoveryPlanner()
        self._probe = probe
        self._workers = max(1, int(workers))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._executor: ThreadPoolExecutor | None = None
        self.last_stats: dict = {}

    def tick(self) -> dict:
        desired = self._desired_provider() or {}
        if not desired:
            return {"down": 0, "probed": 0, "alive": 0, "kicked": 0, "failed": 0}
        client = self._client_factory()
        try:
            runtime = client.runtime_paths()
        except MediaMTXError:
            # Control API молчит — это `unknown`, см. down_paths(). Ошибку
            # уже логирует publish_record_layer_status() раз в 10 с; здесь
            # повтор раз в секунду только зашумил бы журнал.
            logger.debug("супервизор: Control API недоступен", exc_info=True)
            return {"down": 0, "probed": 0, "alive": 0, "kicked": 0, "failed": 0,
                    "api_error": True}
        stats = recover_once(client, desired, runtime, self.planner,
                             time.monotonic(), probe=self._probe,
                             executor=self._executor)
        self.last_stats = stats
        return stats

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                logger.error("ошибка супервизора восстановления потоков",
                             exc_info=True)
            self._stop.wait(self.interval)

    def start(self) -> "RecoverySupervisor":
        self._executor = ThreadPoolExecutor(max_workers=self._workers,
                                            thread_name_prefix="rtsp-probe")
        self._thread = threading.Thread(target=self._run, name="record-recovery",
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                logger.warning("супервизор восстановления не остановился в срок")
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    def snapshot(self) -> dict:
        """Что показать оператору: по камерам в обрыве — сколько длится и
        отвечает ли камера на RTSP.

        Именно это отличает «камера выключена» от «камера отвечает, а
        запись всё равно не идёт», и без строки в интерфейсе эти два
        случая на стене камер выглядят одинаково.
        """
        return self.planner.snapshot(time.monotonic())
