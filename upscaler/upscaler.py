"""
FaceWatch upscaler: фоновый сервис нейросетевого улучшения скриншотов лиц.

Читает Redis-очередь `upscale:queue`, для каждого события:
- берёт исходный скриншот, прогоняет через GFPGAN (восстановление/апскейл лица);
- сохраняет улучшенную версию рядом с оригиналом (оригинал остаётся);
- обновляет FaceEvent.snapshot_path + enhanced=True, при необходимости — аватар персоны;
- публикует `faces:enhanced` для обновления Стены и карточек в реальном времени.

Если тяжёлая модель недоступна, используется надёжный OpenCV-fallback
(Lanczos-апскейл + деноизинг + повышение резкости), чтобы пайплайн всегда работал.
"""
import os
import signal
import threading
import json

import cv2
import numpy as np
import redis
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base

from logging_utils import configure_logging

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
UPSCALE_BACKEND = os.environ.get("UPSCALE_BACKEND", "gfpgan")  # gfpgan|opencv
GFPGAN_MODEL_URL = os.environ.get(
    "GFPGAN_MODEL_URL",
    "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/GFPGANv1.4.pth",
)

logger = configure_logging("facewatch.upscaler")

# Сигнал остановки. Апскейл — единственный сервис проекта, у которого до
# цикла 21 не было graceful shutdown: `main()` крутил `while True`, и
# `docker compose stop` глушил его SIGKILL'ом через grace period. Убить его
# посреди `process_event()` — это осиротевший `enh_*.jpg` на диске (файл
# записан, фаза 3 не дошла до `ev.snapshot_path = enh_rel`) плюс
# невозвращённое в пул соединение.
shutdown_event = threading.Event()


def handle_shutdown_signal(signum, _frame):
    logger.info("получен сигнал, начинаю graceful shutdown", extra={"signum": signum})
    shutdown_event.set()


# pool_size=2 вместо дефолтных 5+10: сервис однопоточный, больше одного
# соединения одновременно ему не нужно никогда — второе оставлено на
# случай, если `pool_pre_ping` признает текущее мёртвым. Дефолт означал,
# что при каждом всплеске ошибок и переоткрытий пул мог держать до 15
# бэкендов Postgres под сервис, которому хватает одного; на целевом железе
# (N100, `max_connections` по умолчанию) это отнимало слоты у backend'а.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=2, max_overflow=1)
Session = sessionmaker(bind=engine)
Base = declarative_base()
r = redis.from_url(REDIS_URL, decode_responses=True)


class Person(Base):
    __tablename__ = "persons"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    status = Column(String)
    avatar_path = Column(String)


class FaceEvent(Base):
    __tablename__ = "face_events"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer)
    person_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    ts = Column(DateTime)
    snapshot_path = Column(String)
    orig_snapshot_path = Column(String)
    enhanced = Column(Boolean)


# ---------------------------------------------------------------------------
# Бэкенды улучшения

_gfpgan = None


def _load_gfpgan():
    global _gfpgan
    if _gfpgan is not None:
        return _gfpgan
    from gfpgan import GFPGANer
    _gfpgan = GFPGANer(
        model_path=GFPGAN_MODEL_URL,
        upscale=2,
        arch="clean",
        channel_multiplier=2,
        bg_upsampler=None,
    )
    return _gfpgan


def enhance_gfpgan(img: np.ndarray) -> np.ndarray:
    restorer = _load_gfpgan()
    _, _, restored = restorer.enhance(img, has_aligned=False, only_center_face=True, paste_back=True)
    return restored if restored is not None else enhance_opencv(img)


def enhance_opencv(img: np.ndarray) -> np.ndarray:
    """Fallback без нейросети: апскейл x2 + лёгкий деноизинг + нерезкое маскирование."""
    h, w = img.shape[:2]
    up = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    den = cv2.fastNlMeansDenoisingColored(up, None, 3, 3, 7, 21)
    blur = cv2.GaussianBlur(den, (0, 0), 2.0)
    sharp = cv2.addWeighted(den, 1.5, blur, -0.5, 0)
    return sharp


def enhance(img: np.ndarray) -> tuple[np.ndarray, str]:
    if UPSCALE_BACKEND == "gfpgan":
        try:
            return enhance_gfpgan(img), "gfpgan"
        except Exception:
            logger.warning("GFPGAN недоступен, fallback на OpenCV", exc_info=True)
    return enhance_opencv(img), "opencv"


# ---------------------------------------------------------------------------

def abspath(rel: str) -> str:
    return os.path.join(MEDIA_PATH, rel)


def process_event(event_id: int, force: bool = False):
    """Улучшение снимка одного события: чтение → инференс → запись.

    Три фазы, и граница между ними — требование к 24/7-работе, а не
    стилистика: **сессия БД не должна быть открыта во время улучшения
    картинки.** Раньше всё стояло внутри одного `with Session()`, и
    транзакция, открытая первым же `s.get(FaceEvent, ...)`, жила до
    `s.commit()` — то есть сквозь `cv2.imread`, весь прогон GFPGAN и
    `cv2.imwrite`.

    Это дольше, чем звучит: апскейл идёт под `os.nice(10)` (ТЗ 18.6 —
    фоновые задачи не конкурируют с детекцией за CPU), поэтому на целевом
    железе (N100) секунды на кадр — норма, а OpenCV-fallback с
    `fastNlMeansDenoisingColored` немногим быстрее GFPGAN. Всё это время
    строка `face_events` оставалась заблокированной на запись, и `DELETE`
    из ротации (`worker.cleanup_old`) ждал её, вместо того чтобы чистить
    архив. Плюс постоянно открытая транзакция не даёт autovacuum убирать
    мёртвые версии строк — при пуле по умолчанию (5 соединений) этого
    сервиса хватало, чтобы держать горизонт видимости открытым почти
    всегда.
    """
    # --- Фаза 1: БД. Что улучшать. -------------------------------------
    with Session() as s:
        ev = s.get(FaceEvent, event_id)
        if not ev:
            return
        if ev.enhanced and not force:
            return  # уже улучшено; повтор только по явному запросу
        src_rel = ev.orig_snapshot_path or ev.snapshot_path
        if not src_rel:
            return
        prev_snapshot = ev.snapshot_path
        person_id = ev.person_id

    # --- Фаза 2: инференс. Открытой сессии БД здесь нет. ---------------
    src = abspath(src_rel)
    if not os.path.exists(src):
        logger.warning("нет исходного файла снимка", extra={"event_id": event_id, "path": src})
        return
    img = cv2.imread(src)
    if img is None:
        return

    out_img, backend = enhance(img)

    name = os.path.basename(src_rel)
    enh_rel = f"snapshots/enh_{name}"
    cv2.imwrite(abspath(enh_rel), out_img, [cv2.IMWRITE_JPEG_QUALITY, 92])

    # --- Фаза 3: БД. Результат. ----------------------------------------
    with Session() as s:
        ev = s.get(FaceEvent, event_id)
        if ev is None:
            # Пока шёл апскейл, событие удалила ротация по сроку хранения.
            # Раньше такого исхода не бывало: открытая транзакция держала
            # строку, и удаление ждало. Теперь оно проходит, а улучшенный
            # файл остаётся сиротой — убираем сразу, не дожидаясь, пока
            # его подберёт очистка по mtime.
            try:
                os.remove(abspath(enh_rel))
            except OSError:
                pass
            logger.info("событие удалено во время апскейла", extra={"event_id": event_id})
            return
        ev.snapshot_path = enh_rel
        ev.enhanced = True

        # Обновляем аватар персоны, если он указывал на исходный/прежний снимок
        person = s.get(Person, person_id) if person_id else None
        if person and person.avatar_path in (src_rel, prev_snapshot, None):
            person.avatar_path = enh_rel
        s.commit()

    # --- Публикация. Сессия уже закрыта. -------------------------------
    try:
        r.publish("faces:enhanced", json.dumps({
            "type": "enhanced",
            "event_id": event_id,
            "person_id": person_id,
            "snapshot": enh_rel,
            "backend": backend,
        }))
    except Exception:
        logger.warning("не удалось опубликовать faces:enhanced", exc_info=True,
                       extra={"event_id": event_id})
    logger.info("событие улучшено", extra={"event_id": event_id, "backend": backend})


def _lower_priority():
    """Апскейл — фоновая задача, не должна конкурировать с детекцией за CPU
    (ТЗ 18.6: "все фоновые задачи... выполняются с низким приоритетом
    процесса"). В Linux-контейнере это nice, на Windows os.nice отсутствует."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def main():
    _lower_priority()
    logger.info("старт апскейлера", extra={"backend": UPSCALE_BACKEND})
    if UPSCALE_BACKEND == "gfpgan":
        try:
            _load_gfpgan()
            logger.info("модель GFPGAN загружена")
        except Exception:
            logger.warning("не удалось загрузить GFPGAN заранее", exc_info=True)
    # Таймаут blpop — верхняя граница задержки реакции на SIGTERM: пока
    # висит блокирующее чтение очереди, установленный из обработчика сигнала
    # флаг не проверяется. 2с с запасом укладываются в grace period
    # `docker compose stop` (10с по умолчанию).
    while not shutdown_event.is_set():
        try:
            item = r.blpop("upscale:queue", timeout=2)
            if not item:
                continue
            payload = json.loads(item[1])
            eid = payload.get("event_id")
            if eid is not None:
                process_event(int(eid), force=bool(payload.get("force")))
        except Exception:
            logger.error("ошибка обработки задачи апскейла", exc_info=True)
            # Прерываемая пауза: на нерабочем Redis/БД сервис не должен
            # игнорировать сигнал остановки лишнюю секунду на каждой итерации.
            shutdown_event.wait(1)

    # Освобождение ресурсов — то, ради чего graceful shutdown и нужен:
    # соединения пула закрываются штатным `terminate`, а не остаются
    # висеть на сервере до `tcp_keepalives_idle`.
    logger.info("завершение: закрываю соединения")
    try:
        engine.dispose()
    except Exception:
        logger.warning("не удалось закрыть пул соединений БД", exc_info=True)
    try:
        r.close()
    except Exception:
        logger.warning("не удалось закрыть соединение Redis", exc_info=True)
    logger.info("апскейлер остановлен")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    # Стартовая пауза (БД/Redis поднимаются параллельно) — тоже прерываемая:
    # SIGTERM в первые 8 секунд после `docker compose up` не должен ждать
    # их истечения.
    shutdown_event.wait(8)
    if not shutdown_event.is_set():
        main()
