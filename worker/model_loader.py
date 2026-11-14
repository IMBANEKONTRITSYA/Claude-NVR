"""Загрузка модели аналитики вне нити менеджера (SPEC §2, §15).

**Зачем.** §2 требует от слоёв не просто независимости при отказе, а
отсутствия общих узких мест: «Слои не имеют общих узких мест: падение/
**деградация** одного не затрагивает другой». §15 говорит то же про
профили производительности ещё определённее: «Профиль применяется ТОЛЬКО
к слою аналитики, **запись не затрагивается**».

Цикл 22 закрыл здесь ровно половину: `load_face_app()` был обёрнут в
`_try_load_model()`, и отказ загрузки перестал ронять процесс вместе со
слоем записи. Это лечит **падение**. Оставалась **деградация**: загрузка
вызывалась синхронно из `manager()` — из той самой нити, которая в том же
проходе синхронизирует пути MediaMTX, индексирует сегменты, публикует
статусы потоков, считает пропуски записи и держит циклическую перезапись
при заполнении диска. Пока загрузка идёт, весь этот управляющий контур
слоя записи стоит.

**Насколько долго — величина известна из самого проекта.** `liveness.py`
отводит этапу `model_load` бюджет **900 секунд**: пятнадцать минут, после
которых сторож считает менеджер зависшим. То есть пятнадцатиминутная
остановка управляющего контура записи была не аварией, а санкционированной
нормой.

**Почему это не редкость.** `insightface` тянет модель через
`requests.get(url, stream=True)` **без единого таймаута** (см.
`insightface/utils/download.py`): ни на соединение, ни на чтение. Три
штатных для объекта случая:

* Первый запуск (§26 режим 2, свежая установка .deb). Пока пак модели
  качается, ни один путь камеры в MediaMTX ещё не создан — то есть архив
  не пишется вовсе. §19 при этом требует «0 пропущенных сегментов».
* Изолированный сервер за файрволом с политикой DROP (для NVR это норма,
  а не экзотика). Соединение не отвергается, а пропадает: ядро исчерпывает
  бюджет ретраев SYN (`tcp_syn_retries`, по умолчанию 6 → ~127 с) на
  каждой попытке. Повтор идёт каждые `MODEL_RETRY_SEC` = 300 с, поэтому
  контур записи стоит примерно две минуты из каждых пяти — **постоянно**.
* Соединение установилось и встало на чтении (прокси, throttling). Без
  read-таймаута это не кончается **никогда**, и сторож здесь тоже не
  поможет: бюджет `model_load` он честно отсчитывает, но убийство процесса
  ради аналитики перезапустило бы и слой записи — то есть §2 нарушался бы
  и лечением тоже.

**Решение.** Загрузка уезжает в собственную нить. Менеджер только
объявляет, какая модель ему нужна (`request()`), и никогда не ждёт
результата. Нить загрузки:

* грузит модель и повторяет попытку после отказа с интервалом `retry_sec`;
* немедленно переходит к новым параметрам, если менеджер запросил другие
  (смена профиля в админке не должна ждать конца паузы повтора);
* держит наблюдаемое состояние (`snapshot()`), которое уезжает в §9:
  «модель грузится 40 с» и «модель не загрузилась» — разные строки для
  дежурного, а до этого цикла обе выглядели как «модель не загружена».

**Нить демонская и без join'а намеренно.** Прервать `requests.get()` без
таймаута нельзя ничем, кроме выхода процесса; ждать её на остановке
значило бы вернуть ту же блокировку в путь завершения. Ресурсов за собой
нить не держит: единственный её эффект — присваивание готовой модели.

Модуль на голом stdlib: он обязан работать в воркере рядом с
cv2/insightface/onnxruntime и одновременно проверяться лёгкой джобой CI,
которая их не ставит (тот же приём, что в `liveness.py`).
"""
from __future__ import annotations

import threading
import time

from logging_utils import configure_logging

logger = configure_logging("facewatch.worker.model_loader")

# Состояния нити загрузки, как их видит §9.
IDLE = "idle"          # параметров ещё не запрашивали
LOADING = "loading"    # попытка идёт прямо сейчас
READY = "ready"        # запрошенная модель загружена
ERROR = "error"        # последняя попытка провалилась, ждём повтора

# Через сколько секунд загрузки писать в журнал предупреждение. Загрузка
# сама по себе долгая (первый запуск качает пак из интернета), поэтому это
# не авария — но молчать о ней нельзя: без строки в журнале «аналитика не
# поднялась» и «аналитика ещё поднимается» неразличимы.
SLOW_LOAD_WARN_SEC = 120.0


class ModelLoader(threading.Thread):
    """Нить, которая держит модель аналитики в соответствии с запросом.

    `load(params) -> bool` — единственная зависимость от воркера: она
    делает саму загрузку и сообщает, удалась ли. Причину отказа модуль не
    формулирует сам, а берёт у `error_getter()` — текст отказа принадлежит
    воркеру (`MODEL_ERROR`), и дублировать его здесь значило бы завести
    второй источник истины для одной строки интерфейса.
    """

    def __init__(self, load, *, error_getter=None, retry_sec: float = 300.0,
                 stop_event: threading.Event | None = None,
                 poll_sec: float = 1.0, clock=time.monotonic):
        super().__init__(name="facewatch-model-loader", daemon=True)
        self._load = load
        self._error_getter = error_getter or (lambda: None)
        self._retry_sec = retry_sec
        self._stop_event = stop_event or threading.Event()
        self._poll_sec = poll_sec
        self._clock = clock

        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._wanted = None          # что просит менеджер
        self._loaded = None          # что удалось загрузить
        self._failed = None          # параметры последней провалившейся попытки
        self._failed_at: float | None = None
        self._state = IDLE
        self._state_since = clock()
        self._warned_slow = False

    # --- интерфейс менеджера ---------------------------------------------

    def request(self, params) -> None:
        """Объявить нужные параметры модели. Никогда не блокирует.

        Вызывается на каждом проходе менеджера, поэтому обязана быть
        дешёвой и молчаливой при неизменных параметрах.
        """
        with self._lock:
            if params == self._wanted:
                return
            self._wanted = params
            # Новые параметры снимают паузу повтора: администратор сменил
            # профиль в админке и вправе ждать применения, а не конца
            # пятиминутного окна, отсчитываемого для ПРОШЛОЙ модели.
            if params != self._failed:
                self._failed = None
                self._failed_at = None
        self._wake.set()

    def snapshot(self) -> dict:
        """Состояние для §9: что грузится, сколько уже, чем кончилось.

        `model` — имя модели, о которой идёт речь: при `loading` это та,
        что грузится сейчас, иначе последняя удавшаяся. Без него строка
        «грузится 40 с» не отвечает на вопрос «что именно», а при смене
        профиля в админке это как раз главный вопрос.
        """
        with self._lock:
            state, since = self._state, self._state_since
            params = self._wanted if state == LOADING else self._loaded
        return {
            "state": state,
            "seconds": round(self._clock() - since, 1),
            "model": params[0] if isinstance(params, (tuple, list)) and params else None,
            "error": self._error_getter() if state == ERROR else None,
        }

    def log_if_slow(self) -> bool:
        """Одно предупреждение на затянувшуюся попытку. True — написали.

        Зовётся из нити менеджера, а не из своей: во время долгой загрузки
        нить загрузки сидит внутри `requests.get()` без таймаута и ничего
        написать не может — именно в этом и состоит разбираемый здесь
        класс отказа. Менеджер же на то и освобождён от ожидания, что
        продолжает крутиться каждые 10 секунд.
        """
        with self._lock:
            slow = (self._state == LOADING
                    and not self._warned_slow
                    and self._clock() - self._state_since > SLOW_LOAD_WARN_SEC)
            if not slow:
                return False
            self._warned_slow = True
            elapsed = self._clock() - self._state_since
        logger.warning(
            "модель распознавания грузится дольше обычного — слой аналитики "
            "ещё не поднят; запись при этом идёт (SPEC §2)",
            extra={"seconds": round(elapsed, 1)},
        )
        return True

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    # --- нить -------------------------------------------------------------

    def _due(self, now: float) -> object | None:
        """Параметры, которые пора грузить, либо None."""
        with self._lock:
            wanted, loaded = self._wanted, self._loaded
            failed, failed_at = self._failed, self._failed_at
        if wanted is None or wanted == loaded:
            return None
        if wanted == failed and failed_at is not None and now - failed_at < self._retry_sec:
            return None
        return wanted

    def _attempt(self, params) -> None:
        started = self._clock()
        with self._lock:
            self._state, self._state_since = LOADING, started
            self._warned_slow = False
        ok = False
        try:
            ok = bool(self._load(params))
        except Exception:
            # `_try_load_model()` не выпускает исключений наружу, но нить
            # обязана пережить и то, чего «не бывает»: её смерть означала бы
            # аналитику, молча выключенную до перезапуска процесса.
            logger.error("нить загрузки модели: необработанный отказ", exc_info=True)
        elapsed = self._clock() - started
        with self._lock:
            if ok:
                self._loaded, self._failed, self._failed_at = params, None, None
                self._state, self._state_since = READY, self._clock()
            else:
                self._failed, self._failed_at = params, self._clock()
                self._state, self._state_since = ERROR, self._clock()
        logger.info("попытка загрузки модели завершена",
                    extra={"ok": ok, "seconds": round(elapsed, 1)})

    def run(self) -> None:
        while not self._stop_event.is_set():
            params = self._due(self._clock())
            if params is not None:
                self._attempt(params)
                continue
            self._wake.wait(self._poll_sec)
            self._wake.clear()
