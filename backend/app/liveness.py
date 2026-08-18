"""Сторож живости бэкенда — заблокированный цикл событий (§13/§19).

**Зачем.** §13 требует «авторестарт сервисов при падении», §19 — RTO ≤ 5
минут. Оба слагаемых закрывали ровно один класс отказа: смерть процесса.
`restart: unless-stopped` (режим 1 §26) и `Restart=always` (режим 2)
реагируют на выход процесса — и только на него.

Класс «сервис жив и не отвечает» не закрывал никто. **Docker не
перезапускает контейнер по проваленному healthcheck** — он лишь помечает
его `unhealthy`, на `restart:` это не влияет. Заблокированный бэкенд
оставался в этом состоянии сколько угодно: интерфейс не открывается, live
не идёт, а супервизор считает, что всё в порядке, потому что процесс жив.

`/api/health` у бэкенда, в отличие от воркера, не врал никогда — он ходит
в Postgres и Redis и отдаёт 503 (см. `main.py`). Не хватало не честного
ответа, а **моста от «нездоров» к «перезапущен»**. Этот модуль его и есть.

**Что именно ловится.** Не «БД недоступна» — это состояние снаружи, его
рестарт не чинит, и на него уже отвечает `/api/health`. Ловится
**заблокированный цикл событий**: синхронный вызов в async-обработчике
(чтение файла архива, CPU-тяжёлая ветка), дедлок в C-расширении, вставший
том. В этом состоянии не отвечает вообще ничто, включая сам healthcheck.

**Как.** Фоновая корутина отмечается раз в секунду. Проверяет отметку
**нить**, а не другая корутина: заблокированный цикл не выполнит корутину
по определению — сторож, живущий внутри того, за кем следит, при отказе
замолчал бы вместе с ним. Нить же продолжает работать: GIL отпускается на
`Event.wait()`, а блокировка цикла событий его не держит.

Два порога, как и у воркера (`worker/liveness.py`): бюджет отставания —
цикл считается вставшим, отсрочка — после неё процесс убивается. Разрыв
нужен, чтобы разовый долгий синхронный кусок (миграция, выгрузка отчёта)
приводил к записи в журнале, а не к рестарту.

RTO класса «зависание» = бюджет + отсрочка + рестарт + прогрев. На
умолчаниях 60 + 60 + ~0.1 + прогрев ≈ 2 минуты, внутри §19 «≤ 5 минут».
Замер — `perf/bench_rto.py`.
"""
import asyncio
import faulthandler
import os
import sys
import threading
import time

from .logging_utils import configure_logging

logger = configure_logging("facewatch.backend.liveness")

# Код выхода при срабатывании сторожа: отличен и от 0 (штатное завершение),
# и от 1 (необработанное исключение), чтобы «перезапущен сторожем» было
# видно в `docker inspect`/journal, а не сливалось с обычным падением.
EXIT_STALLED = 17

# Отставание цикла событий, после которого он считается вставшим. Здоровый
# бэкенд отстаёт на десятки миллисекунд; десятки секунд — это уже не
# нагрузка, а блокировка, при которой сервис всё равно не обслуживает.
DEFAULT_LAG_BUDGET_SEC = 60.0
DEFAULT_KILL_GRACE_SEC = 60.0

# Как часто отмечается корутина. Секунда — на два порядка меньше бюджета,
# то есть в разрешение замера отставание не упирается.
BEAT_INTERVAL_SEC = 1.0


def _env_float(name: str, default: float) -> float:
    """Числовая настройка из окружения; мусор не роняет бэкенд, а логируется.

    Механизм, добавленный ради надёжности, не имеет права стать новой
    причиной отказа — в том числе из-за опечатки в `.env`.
    """
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("нечисловое значение переменной окружения, беру умолчание",
                       extra={"variable": name, "value": raw, "default": default})
        return default


class LoopHeartbeat:
    """Отметка цикла событий о том, что он ещё крутится."""

    def __init__(self, clock=time.monotonic):
        # monotonic: перевод системных часов (первая синхронизация ntp на
        # объекте без интернета) не должен ни «омолаживать» отметку, ни
        # ронять процесс скачком вперёд.
        self._clock = clock
        self._lock = threading.Lock()
        self._at = self._clock()

    def beat(self) -> None:
        with self._lock:
            self._at = self._clock()

    def lag(self) -> float:
        """Секунд с последней отметки цикла событий."""
        with self._lock:
            at = self._at
        return self._clock() - at


async def beat_loop(hb: LoopHeartbeat, stop: asyncio.Event,
                    interval: float = BEAT_INTERVAL_SEC) -> None:
    """Корутина-отметчик. Живёт весь срок жизни приложения (`lifespan`)."""
    while not stop.is_set():
        hb.beat()
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
    # Финальная отметка: остановка отметчика при завершении не должна
    # выглядеть для сторожа как вставший цикл.
    hb.beat()


class LoopWatchdog(threading.Thread):
    """Нить, убивающая процесс, если цикл событий встал дольше бюджета."""

    def __init__(self, hb: LoopHeartbeat, *,
                 lag_budget_sec: float = DEFAULT_LAG_BUDGET_SEC,
                 kill_grace_sec: float = DEFAULT_KILL_GRACE_SEC,
                 interval_sec: float = 1.0, on_trip=None, clock=time.monotonic):
        super().__init__(name="facewatch-loop-watchdog", daemon=True)
        self._hb = hb
        self._budget = lag_budget_sec
        self._kill_grace = kill_grace_sec
        self._interval = interval_sec
        self._on_trip = on_trip or _die
        self._clock = clock
        self._stop_event = threading.Event()
        self._stalled_since: float | None = None
        self._warned = False

    def stop(self) -> None:
        self._stop_event.set()

    def check_once(self) -> bool:
        """Один проход проверки. True — сторож сработал.

        Отдельным методом ради теста: проверять решение сторожа, гоняя нить
        и ожидая реальные минуты бюджета, в CI нельзя.
        """
        lag = self._hb.lag()
        if lag <= self._budget:
            if self._stalled_since is not None:
                logger.info("цикл событий снова крутится, сторож отступает",
                            extra={"lag_sec": round(lag, 1)})
            self._stalled_since = None
            self._warned = False
            return False

        now = self._clock()
        if self._stalled_since is None:
            self._stalled_since = now
        if not self._warned:
            self._warned = True
            logger.error("цикл событий заблокирован — бэкенд не обслуживает запросы",
                         extra={"lag_sec": round(lag, 1), "budget_sec": self._budget,
                                "kill_in_sec": round(self._kill_grace, 1)})
        if now - self._stalled_since < self._kill_grace:
            return False

        logger.critical("цикл событий бэкенда заблокирован — перезапуск процессом "
                        "сторожа (SPEC §13: авторестарт сервисов)",
                        extra={"lag_sec": round(lag, 1), "budget_sec": self._budget,
                               "exit_code": EXIT_STALLED})
        self._on_trip(lag)
        return True

    def run(self) -> None:
        while not self._stop_event.is_set():
            try:
                if self.check_once():
                    return
            except Exception:
                # Сторож не имеет права упасть: без него класс отказа
                # «зависание» снова не закрыт ничем, а падение нити тихое.
                logger.error("ошибка в нити сторожа живости", exc_info=True)
            self._stop_event.wait(self._interval)


def _die(lag: float) -> None:
    """Свалить стеки всех нитей и выйти из процесса.

    Стеки — единственный шанс узнать, что именно заблокировало цикл на
    боевом сервере: после рестарта улика исчезает. `faulthandler` пишет их
    сырым текстом в stderr, потому что работает из любого состояния
    процесса — в том числе когда логирование упёрлось в тот же ресурс.

    `os._exit`, а не `sys.exit`: штатный выход идёт через `lifespan` и
    завершение задач, то есть через тот самый вставший цикл событий.
    """
    try:
        print(f"--- facewatch watchdog: цикл событий заблокирован {lag:.1f} с, "
              f"стеки всех нитей ниже ---", file=sys.stderr, flush=True)
        faulthandler.dump_traceback(file=sys.stderr, all_threads=True)
        sys.stderr.flush()
    except Exception:
        pass
    os._exit(EXIT_STALLED)


def start_watchdog(hb: LoopHeartbeat) -> LoopWatchdog | None:
    """Поднять сторожа по настройкам окружения. None — сторож выключен.

    Выключается `BACKEND_WATCHDOG_ENABLED=0`: на объекте может стоять
    внешний супервизор (systemd `WatchdogSec` в режиме 2 §26), и два
    механизма, считающих одно и то же по-разному, хуже одного. Тесты
    выключают его же переменной — TestClient гоняет цикл событий рывками,
    и сторож там мерил бы паузы между тестами.
    """
    if os.environ.get("BACKEND_WATCHDOG_ENABLED", "1").strip().lower() in ("0", "false", "no"):
        logger.warning("сторож живости бэкенда выключен (BACKEND_WATCHDOG_ENABLED=0)")
        return None
    wd = LoopWatchdog(
        hb,
        lag_budget_sec=_env_float("BACKEND_WATCHDOG_LAG_BUDGET_SEC", DEFAULT_LAG_BUDGET_SEC),
        kill_grace_sec=_env_float("BACKEND_WATCHDOG_KILL_GRACE_SEC", DEFAULT_KILL_GRACE_SEC),
    )
    wd.start()
    logger.info("сторож живости бэкенда запущен",
                extra={"lag_budget_sec": wd._budget, "kill_grace_sec": wd._kill_grace})
    return wd
