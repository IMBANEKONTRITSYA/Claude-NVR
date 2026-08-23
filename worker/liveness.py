"""Сторож живости менеджера воркера — класс отказа «сервис жив и не отвечает».

**Зачем.** §13 требует «авторестарт сервисов при падении», §19 задаёт
RTO ≤ 5 минут. И то и другое до сих пор закрывало ровно один класс отказа —
**смерть процесса**: `restart: unless-stopped` в docker-compose (режим 1 §26)
и `Restart=always` в systemd (режим 2) реагируют на выход процесса. Второй
класс — **зависание** — не закрывал никто:

* Docker **не перезапускает контейнер по проваленному healthcheck.** Он лишь
  помечает его `unhealthy`; на `restart:` это не влияет никак. Контейнер с
  намертво вставшим менеджером живёт `unhealthy` вечно.
* Хуже: у воркера healthcheck в этом состоянии **не провалился бы вовсе**.
  `/health` обслуживает uvicorn в отдельной нити (`embed_api.py`), и она
  отвечает `{"ok": true}` независимо от того, крутится ли `manager()`.
  Снаружи зависший воркер выглядел здоровым, пока слой записи не
  синхронизировался, сегменты не индексировались, retention и циклическая
  перезапись не работали.

Чем зависает менеджер на практике: `Session()` уходит в Postgres по
молчаливо умершему TCP (NAT/файрвол выбросил сессию) и блокируется в
`recv()` **без срока** — `pool_pre_ping` тут не спасает, потому что сам
пинг `SELECT 1` блокируется в том же сокете. Лечение с двух сторон: TCP
keepalive на соединениях (`worker.py`, `backend/app/db.py`) — чтобы ядро
рвало мёртвый сокет само, — и этот сторож как последний рубеж на всё
остальное (дедлок, вставший NFS-том архива, зацикливание).

**Как устроено.** Менеджер отмечается перед каждым этапом прохода
(`beat("record_layer_sync")`). Сторож — отдельная нить, которая раз в
секунду смотрит, сколько прошло с последней отметки, и сравнивает с
бюджетом **этого этапа**: у синхронизации слоя записи и у часовой уборки
архива законные времена отличаются на порядок, и один общий порог пришлось
бы задирать до бесполезного.

Два порога, а не один, и оба нужны:

* **`stalled`** (бюджет этапа) — этап завис. `/health` отдаёт 503 с именем
  этапа, healthcheck контейнера краснеет, оператор видит причину. Процесс
  при этом ещё жив — зависание может рассосаться (том вернулся, запрос
  дошёл), и убивать сразу значило бы менять простой на рестарт там, где
  можно обойтись без него.
* **`kill`** (`stalled` + `WORKER_WATCHDOG_KILL_GRACE_SEC`) — не рассосалось.
  Сторож сваливает стеки **всех** нитей в лог и выходит из процесса, а
  супервизор поднимает его заново. Стеки — не роскошь: это единственный
  шанс узнать, на чём именно встал боевой воркер, потому что после
  рестарта улика исчезает.

Выход — `os._exit()`, не `sys.exit()` и не `shutdown_event.set()`. Штатное
завершение проходит через тот самый код, который завис (менеджер джойнит
нити камер, дописывает индекс сегментов), — на зависшем процессе оно
повисло бы вместе с ним. Сторож для того и нужен, чтобы не зависеть от
здоровья того, кого он сторожит.

RTO класса «зависание» = `stalled` + `kill_grace` + рестарт + прогрев.
На умолчаниях это 120 + 60 + ~0.1 + прогрев ≈ 3 минуты — внутри §19 «≤ 5
минут» с запасом. Замер — `perf/bench_rto.py --hang`.

Модуль намеренно на голом stdlib: он обязан работать в воркере, где рядом
живут cv2/insightface/onnxruntime, и одновременно проверяться лёгкой
джобой CI, которая их не ставит.
"""
import faulthandler
import os
import sys
import threading
import time

from logging_utils import configure_logging

logger = configure_logging("facewatch.worker.liveness")

# Код выхода при срабатывании сторожа. Отличен и от 0 (штатное завершение),
# и от 1 (необработанное исключение) — чтобы «воркер перезапущен сторожем»
# было видно в `docker inspect`/journal, а не сливалось с обычным падением.
EXIT_STALLED = 17

# Бюджеты этапов прохода менеджера, секунды. Не «сколько этап обычно
# занимает», а «после какого времени это уже точно не работа, а зависание»:
# нижняя граница выбрана с запасом к худшему законному случаю.
DEFAULT_BUDGET_SEC = 120.0
STAGE_BUDGETS_SEC: dict[str, float] = {
    # Проход по камерам: расшифровка адресов + подъём нитей аналитики.
    # Чистый CPU и Postgres, секунды даже на 250 камерах.
    "camera_scan": 120.0,
    # Синхронизация путей в MediaMTX: сеть. У клиента Control API свой
    # таймаут 10 с на запрос (record_layer.py), но запросов до одного на
    # камеру — на 250 камерах худший законный случай уже минуты.
    "record_layer_sync": 600.0,
    "index_segments": 300.0,
    "record_status": 120.0,
    # Уборка архива по retention: удаление файлов сегментов. На объекте это
    # десятки тысяч unlink() по HDD, и «медленно» здесь — норма, а не сбой.
    "cleanup": 900.0,
    "motion_prune": 900.0,
    "disk_quota": 900.0,
    "disk_alerts": 120.0,
    # Этапа `model_load` здесь больше нет, и это не потеря покрытия.
    #
    # Он стоял тут с бюджетом 900 секунд — «при первом запуске модель
    # скачивается из интернета». Пока загрузка шла синхронно в нити
    # менеджера, число означало вот что: пятнадцать минут, в течение
    # которых слой записи не синхронизирует пути, не индексирует сегменты,
    # не публикует статусы и не выполняет циклическую перезапись, —
    # считаются нормой. §2 запрещает ровно это («слои не имеют общих узких
    # мест: падение/деградация одного не затрагивает другой»), поэтому с
    # цикла 55 загрузка ушла в свою нить (model_loader.py), а менеджер
    # больше не отмечается этим этапом ни разу.
    #
    # Сторожить нить загрузки этот сторож и не должен: его реакция —
    # убийство процесса, а убить процесс из-за не поднявшейся аналитики
    # значило бы перезапустить вместе с ней слой записи, то есть нарушить
    # §2 самим лечением. Затянувшаяся загрузка видна иначе: строкой в
    # журнале и состоянием `analytics.load` на странице мониторинга (§9).
    # Ожидание между проходами — ровно 10 с плюс запас на планировщик.
    "idle": 60.0,
    # Завершение: джойн нитей камер ограничен 8 с самим менеджером,
    # последняя индексация — обычный запрос.
    "shutdown": 300.0,
}


def _env_float(name: str, default: float) -> float:
    """Числовая настройка из окружения; мусор не роняет воркер, а логируется.

    Сторож обязан пережить кривую переменную окружения: уронить из-за неё
    процесс значило бы, что механизм, добавленный ради надёжности, стал
    новой причиной отказа.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "нечисловое значение переменной окружения, беру умолчание",
            extra={"variable": name, "value": raw, "default": default},
        )
        return default


class Heartbeat:
    """Отметки менеджера о том, на каком этапе он находится.

    Хранит только последнюю отметку: вопрос, на который сторож отвечает, —
    «сколько менеджер уже сидит в текущем этапе», а не история проходов.
    """

    def __init__(self, budgets: dict[str, float] | None = None,
                 default_budget: float = DEFAULT_BUDGET_SEC,
                 clock=time.monotonic):
        # monotonic, а не time(): перевод системных часов (ntp на объекте
        # без интернета в первый раз синхронизировался) не должен ни
        # «омолаживать» отметку, ни ронять процесс скачком вперёд.
        self._clock = clock
        self._budgets = dict(budgets if budgets is not None else STAGE_BUDGETS_SEC)
        self._default_budget = default_budget
        self._lock = threading.Lock()
        self._stage = "startup"
        self._at = self._clock()

    def beat(self, stage: str) -> None:
        """Отметиться: менеджер входит в этап `stage`."""
        with self._lock:
            self._stage = stage
            self._at = self._clock()

    def budget_for(self, stage: str) -> float:
        return self._budgets.get(stage, self._default_budget)

    def snapshot(self) -> tuple[str, float, float]:
        """`(этап, секунд с отметки, бюджет этапа)` — атомарно.

        Атомарность важна: сторож и обработчик `/health` читают состояние из
        своих нитей, пока менеджер его меняет, и разъехавшаяся пара
        «этап от прошлого прохода, время от нового» дала бы ложное
        срабатывание.
        """
        with self._lock:
            stage, at = self._stage, self._at
        return stage, self._clock() - at, self.budget_for(stage)

    def stalled(self) -> tuple[str, float, float] | None:
        """Тот же снимок, но только если бюджет этапа уже превышен."""
        stage, elapsed, budget = self.snapshot()
        if elapsed > budget:
            return stage, elapsed, budget
        return None


class Watchdog(threading.Thread):
    """Нить, убивающая процесс, если менеджер завис дольше бюджета + отсрочки."""

    def __init__(self, heartbeat: Heartbeat, *, kill_grace_sec: float,
                 interval_sec: float = 1.0, on_trip=None, clock=time.monotonic):
        super().__init__(name="facewatch-watchdog", daemon=True)
        self._hb = heartbeat
        self._kill_grace = kill_grace_sec
        self._interval = interval_sec
        self._on_trip = on_trip or _die
        self._clock = clock
        self._stop_event = threading.Event()
        # Момент, с которого текущий этап числится зависшим. None — здоров.
        self._stalled_since: float | None = None
        # Чтобы «этап завис» не сыпалось в лог каждую секунду отсрочки.
        self._warned_stage: str | None = None

    def stop(self) -> None:
        self._stop_event.set()

    def check_once(self) -> bool:
        """Один проход проверки. True — сторож сработал.

        Вынесен отдельным методом ради теста: проверять решение сторожа,
        гоняя нить и ожидая реальные минуты, нельзя.
        """
        stalled = self._hb.stalled()
        if stalled is None:
            # Этап сменился или уложился — счётчик отсрочки сбрасывается.
            if self._stalled_since is not None:
                stage, _, _ = self._hb.snapshot()
                logger.info("зависание рассосалось, сторож отступает",
                            extra={"stage": stage})
            self._stalled_since = None
            self._warned_stage = None
            return False

        stage, elapsed, budget = stalled
        now = self._clock()
        if self._stalled_since is None:
            self._stalled_since = now
        if self._warned_stage != stage:
            self._warned_stage = stage
            logger.error(
                "этап менеджера превысил бюджет — воркер помечен нездоровым",
                extra={"stage": stage, "elapsed_sec": round(elapsed, 1),
                       "budget_sec": budget,
                       "kill_in_sec": round(self._kill_grace, 1)},
            )
        if now - self._stalled_since < self._kill_grace:
            return False

        logger.critical(
            "менеджер воркера завис — перезапуск процессом сторожа "
            "(SPEC §13: авторестарт сервисов)",
            extra={"stage": stage, "elapsed_sec": round(elapsed, 1),
                   "budget_sec": budget, "exit_code": EXIT_STALLED},
        )
        self._on_trip(stage, elapsed)
        return True

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.check_once():
                    return
            except Exception:
                # Сторож не имеет права упасть: без него класс отказа
                # «зависание» снова не закрыт ничем, а падение нити тихое.
                logger.error("ошибка в нити сторожа", exc_info=True)
            self._stop_event.wait(self._interval)


def _die(stage: str, elapsed: float) -> None:
    """Свалить стеки всех нитей и выйти из процесса.

    Стеки идут в stderr отдельно от JSON-лога: `faulthandler` пишет сырой
    текст и структурировать его нечем, зато он работает из любого состояния
    процесса — в том числе когда логирование само упёрлось в тот же ресурс,
    что и менеджер.
    """
    try:
        print(
            f"--- facewatch watchdog: менеджер завис на этапе {stage!r} "
            f"({elapsed:.1f} с), стеки всех нитей ниже ---",
            file=sys.stderr, flush=True,
        )
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
    except Exception:
        pass
    # _exit, а не sys.exit: штатный выход идёт через atexit и завершение
    # нитей, то есть через зависший код.
    os._exit(EXIT_STALLED)


def start_watchdog(heartbeat: Heartbeat) -> Watchdog | None:
    """Поднять сторожа по настройкам окружения. None — сторож выключен.

    Выключается `WORKER_WATCHDOG_ENABLED=0`: на объекте может стоять внешний
    супервизор (systemd `WatchdogSec` в режиме 2 §26), и два механизма,
    считающих одно и то же по-разному, хуже одного.
    """
    if os.environ.get("WORKER_WATCHDOG_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        logger.warning("сторож живости выключен (WORKER_WATCHDOG_ENABLED=0)")
        return None
    grace = _env_float("WORKER_WATCHDOG_KILL_GRACE_SEC", 60.0)
    wd = Watchdog(heartbeat, kill_grace_sec=grace)
    wd.start()
    logger.info("сторож живости запущен",
                extra={"kill_grace_sec": grace, "default_budget_sec": DEFAULT_BUDGET_SEC})
    return wd
