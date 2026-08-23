"""
FaceWatch worker — слой аналитики (SPEC §2) плюс управление слоем записи.

Слой записи (SPEC §20) воркер **не выполняет сам**: основные потоки всех
камер тянет и пишет remux'ом MediaMTX, воркер только сверяет конфигурацию
его путей через Control API и заносит дописанные сегменты в архив
(record_layer.py, segment_index.py). До цикла 24 запись жила прямо в нити
камеры — по процессу ffmpeg на камеру ради републикации и cv2.VideoWriter с
последующим перекодированием mp4v→H.264, — что противоречило §20 и §24 и
связывало запись с живостью аналитики вопреки §2.

Аналитика по каждой камере:
- детекция движения (MOG2) с применением ROI-маски;
- InsightFace эмбеддинги, кластеризация через ближайший центроид (pgvector);
- сохранение последнего кадра камеры (snapshots/cam{id}_latest.jpg);
- периодическая DBSCAN-перекластеризация для слияния дублирующихся неизвестных;
- ротация архива и медиа по retention_days.
"""
import os
import sys
import time
import json
import uuid
import shlex
import shutil
import signal
import threading
import subprocess
from datetime import datetime, timedelta, timezone

import cv2
import base64
import hashlib
import numpy as np
import redis
from cryptography.fernet import Fernet
from sklearn.cluster import DBSCAN
from sqlalchemy import create_engine, select, text, delete, update, func
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import (Column, Integer, BigInteger, String, DateTime, Boolean,
                        ForeignKey, JSON, Text)
from pgvector.sqlalchemy import Vector

from analytics_source import (SUB_BELOW_FLOOR_NO_MAIN,
                              choose_analytics_source, describe)
from backoff import reconnect_delay
import cpu_affinity
from detection_schedule import schedule_active
from face_select import pick_matching_face
from fileage import prune_media
from liveness import Heartbeat, start_watchdog
from ort_threads import analytics_thread_budget, limit_threads as limit_ort_threads
from avatar_store import adopt_snapshot, avatar_files, needs_avatar
from orphan_media import (remove_files, segment_camera_ids,
                          thumb_segment_ids)
from motion_windows import (DEFAULT_GUARD_SEC, MotionWindowTracker,
                            SETTLE_SEC as MOTION_SETTLE_SEC,
                            segments_without_motion)
from record_layer import (MediaMTXClient, path_conf, path_name,
                          record_media_root, record_path_template,
                          record_root_divergence, redact_url, segments_dir,
                          sync_paths)
from record_status import (SEGMENT_GAP_FACTOR, UNKNOWN, alert_batch,
                           newly_lost, newly_restored, segment_gaps,
                           stream_states, summarize)
from stream_rate import SegmentFpsCache, update_bitrates
from stream_recovery import RecoverySupervisor
from segment_index import index_new_segments
from snapshot_http import fetch_snapshot_bytes
from storage import (BYTES_PER_GB, bytes_to_free, disk_alert_level,
                     expired_segments, oldest_segments_to_free)
from thumbs import drop_thumb
from shutdown import shutdown_event, handle_shutdown_signal
from logging_utils import configure_logging
from hwaccel import hw_decode_requested, detect_hw_accelerator_name
import mailer
import onvif_client

logger = configure_logging("facewatch.worker")

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
# Корень медиаданных глазами MediaMTX (см. record_layer.record_media_root).
# Совпадает с MEDIA_PATH везде, кроме развёртывания, где MediaMTX смонтировал
# тот же том по другому пути и это заявлено через MEDIAMTX_MEDIA_PATH.
RECORD_MEDIA_ROOT = record_media_root()
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
FERNET_KEY = os.environ.get("RTSP_ENCRYPTION_KEY", "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=")
MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "mediamtx")
MEDIAMTX_PORT = int(os.environ.get("MEDIAMTX_PORT", "8554"))
# Control API MediaMTX — им синхронизируются пути слоя записи (SPEC §20).
# Учётка Control API едет внутри адреса (`http://facewatch:пароль@...`):
# MediaMTX не отдаёт `api` анонимно никому, кроме loopback, а воркер — в
# соседнем контейнере. Дефолт совпадает с паролем из mediamtx/mediamtx.yml,
# чтобы `docker compose up` работал без правки .env; в production оба
# значения задаются переменными окружения (см. .env.example).
MEDIAMTX_API_URL = os.environ.get(
    "MEDIAMTX_API_URL", f"http://facewatch:facewatch-mediamtx-api@{MEDIAMTX_HOST}:9997"
)
# Период прохода супервизора восстановления потоков (SPEC §19, см.
# stream_recovery.py). `0` выключает супервизор целиком — на случай, когда
# на объекте пересоздание путей нежелательно и потерю секунд архива после
# обрыва принимают осознанно.
RECORD_RECOVERY_INTERVAL = float(os.environ.get("RECORD_RECOVERY_INTERVAL", "1.0"))

DBSCAN_EPS = 0.35
DBSCAN_MIN_SAMPLES = 3

# Рантайм-конфиг, обновляется из таблицы settings (см. refresh_config)
CONFIG = {
    "retention_days": RETENTION_DAYS,
    "motion_threshold": 1500,
    "similarity_threshold": 0.45,   # 1 - cosine_similarity; ниже — совпадение
    "detection_fps": 10,
    "event_cooldown_sec": 10,
    "alert_cooldown_sec": 300,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
    # SPEC §6 «Алерты при детекции (Telegram, email, звук)». Пустой smtp_host
    # или пустой список получателей = почтовый канал выключен; остальные
    # каналы от этого не зависят.
    "smtp_host": "",
    "smtp_port": 587,
    "smtp_user": "",
    "smtp_password": "",
    "smtp_tls": "starttls",
    "smtp_from": "",
    "alert_email_to": "",
    # Профиль производительности (ТЗ 18)
    "frame_skip": 1,           # анализировать каждый (frame_skip+1)-й обработанный кадр
    "motion_prefilter": 1,     # детектор лиц только по движению
    "idle_fps": 2,             # частота при длительном отсутствии движения
    "face_model": "buffalo_s",
    # SPEC §16, §19: потолок CPU слоя аналитики. 0 — считать автоматически
    # из числа ядер и `analytics_cameras_max` (см. ort_threads.py). Читается
    # воркером, потому что применяется к сессиям ONNX Runtime при загрузке
    # модели, а не запросом к БД.
    "analytics_threads": 0,
    # Разрешённое число камер analytics (то же значение, что у бэкенда в
    # routers/cameras.py). Воркеру нужно как делитель бюджета потоков.
    "analytics_cameras_max": 2,
    "upscale_mode": "avatar",  # manual | avatar | all
    "cluster_interval_min": 15,
    "detect_width": 640,
    # Слой записи (SPEC §20: «Сегменты 5–10 минут»). Кодек/битрейт/GOP
    # больше не настраиваются: запись идёт remux'ом как есть, §24 явно
    # запрещает перекодирование архива.
    "record_segment_min": 5,
    # SPEC §5, §21: циклическая перезапись. Ниже этого процента свободного
    # места сносятся старейшие сегменты независимо от их retention.
    # 5% — запас, которого хватает записи пережить час до следующего
    # прохода уборки на 120 камерах (~30 MB/s → ~108 ГБ/час, то есть 5% от
    # тома в 2 ТБ и выше).
    "disk_min_free_pct": 5,
    # SPEC §14: пороги алертов по заполнению диска.
    "disk_warn_pct": 80,
    "disk_crit_pct": 90,
}

# Типы значений настроек: как приводить строку из БД
_CONFIG_TYPES = {
    "retention_days": int, "motion_threshold": int, "similarity_threshold": float,
    "detection_fps": int, "event_cooldown_sec": int, "alert_cooldown_sec": int,
    "telegram_bot_token": str, "telegram_chat_id": str,
    "smtp_host": str, "smtp_port": int, "smtp_user": str,
    "smtp_password": str, "smtp_tls": str, "smtp_from": str,
    "alert_email_to": str,
    "frame_skip": int, "motion_prefilter": int, "idle_fps": int,
    "face_model": str, "upscale_mode": str, "cluster_interval_min": int,
    "analytics_threads": int, "analytics_cameras_max": int,
    "detect_width": int, "record_segment_min": int,
    "disk_min_free_pct": int, "disk_warn_pct": int, "disk_crit_pct": int,
}

# Секунд без движения, после которых камера уходит в «спящий» режим детекции
IDLE_AFTER_SEC = 20

# Статусы потоков слоя записи с предыдущего прохода менеджера: по ним
# считается ПЕРЕХОД online → offline (SPEC §14 требует алерт на потерю
# потока, а не на факт «сейчас offline» — см. record_status.newly_lost).
_record_prev_status: dict[int, str] | None = None

# Пробы счётчика `bytesReceived` с прошлого прохода: «камера → (время,
# байты)». По ним считается битрейт §9 — Control API отдаёт только
# накопительный счётчик, скорости в нём нет (см. stream_rate.py).
_record_byte_samples: dict[int, tuple[float, int]] = {}

# FPS §9 по последнему дописанному сегменту каждой камеры. Кэш держится
# процессом, а не пересоздаётся на проходе: он и существует ради того,
# чтобы один и тот же файл не пробовался ffprobe каждые пять секунд.
_record_fps_cache = SegmentFpsCache()

# Последняя причина недоступности Control API MediaMTX (None — доступен).
# Хранится, чтобы, во-первых, не повторять одно и то же предупреждение
# каждые 10 секунд, во-вторых — показать причину в «Мониторинге».
_record_api_error: str | None = None

# Расхождение «куда пишет MediaMTX» и «где ищет архив» (None — совпадают).
# Заполняется один раз на старте: обе величины приходят из окружения и в
# течение жизни процесса не меняются.
_record_root_warning: str | None = None

# Желаемая конфигурация путей слоя записи с последнего прохода менеджера.
# Читает её нить супервизора восстановления (stream_recovery.py), которая
# крутится раз в секунду и своей сессии БД не имеет: спрашивать список
# камер у Postgres секундным опросом ради величины, меняющейся раз в
# недели, незачем. Присваивание словаря целиком — атомарная операция, и
# блокировка здесь не нужна: нить либо видит прежний словарь, либо новый,
# но никогда не половину.
_record_desired: dict[str, dict] = {}

# Супервизор восстановления потоков (SPEC §19). Создаётся в manager(),
# отсюда его состояние забирает publish_record_layer_status() для
# интерфейса.
_record_recovery: RecoverySupervisor | None = None


def _pg_connect_args(url: str) -> dict:
    """TCP-настройки соединения с Postgres против бесконечной блокировки.

    Класс отказа, который они закрывают: сессия к Postgres умерла молча —
    NAT/файрвол выбросил запись о соединении, сервер уехал в перезагрузку, —
    сокет остался формально открытым, и `recv()` ждёт **без срока**.
    Менеджер воркера при этом висит в первом же `Session()` и не делает
    больше ничего: ни синхронизации слоя записи, ни индексации сегментов,
    ни retention (см. `liveness.py`).

    `pool_pre_ping` от этого не спасает: его `SELECT 1` уходит в тот же
    мёртвый сокет и блокируется вместе с остальным. Спасает keepalive —
    ядро само рвёт соединение примерно за
    `keepalives_idle + keepalives_interval × keepalives_count` (здесь ~60 с),
    после чего SQLAlchemy получает нормальную ошибку, `pool_pre_ping`
    выбрасывает соединение из пула и следующий запрос идёт по новому.

    `statement_timeout` сознательно **не** задаётся: у воркера есть законно
    долгие запросы (уборка архива по retention удаляет десятки тысяч
    строк), и глобальный срок молча ронял бы их на объекте с большим
    архивом. Долгий запрос — это работа, а не зависание; зависание ловит
    сторож живости, у которого на каждый этап свой бюджет.

    Только для Postgres: тесты слоя записи поднимают SQLite-файл, который
    таких параметров не знает.
    """
    if not url.startswith(("postgresql", "postgres:")):
        return {}
    return {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
    }


# pool_size подобран под целевую нагрузку ТЗ: каждая из до 16 камер держит
# свой поток с короткоживущими сессиями (load_cam_state, запись событий),
# плюс сегментный транскод/рекластеризация в отдельных потоках — дефолтный
# pool_size=5 у SQLAlchemy становится узким местом раньше, чем CPU/сеть.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=20, max_overflow=10,
                       connect_args=_pg_connect_args(DATABASE_URL))
Session = sessionmaker(bind=engine)
Base = declarative_base()
def _normalize_fernet_key(raw: str) -> bytes:
    """Должно совпадать с backend/app/services/encryption.py."""
    raw_bytes = raw.encode()
    try:
        Fernet(raw_bytes)
        return raw_bytes
    except Exception:
        return base64.urlsafe_b64encode(hashlib.sha256(raw_bytes).digest())


fernet = Fernet(_normalize_fernet_key(FERNET_KEY))
r = redis.from_url(REDIS_URL, decode_responses=True)

# Секреты в таблице settings хранятся зашифрованными (ТЗ 13) — см.
# backend/app/services/encryption.py, откуда взяты и префикс, и набор ключей.
# Значение без префикса — legacy-plaintext из БД, развёрнутой до этого фикса;
# backend дошифровывает такие значения при старте, но воркер может прочитать
# настройки раньше, чем это произойдёт, поэтому читает оба вида.
SECRET_SETTING_PREFIX = "enc:v1:"
SECRET_SETTING_KEYS = frozenset({"telegram_bot_token", "smtp_password"})


def _decrypt_setting(stored: str) -> str:
    """Зеркало decrypt_setting() из backend/app/services/encryption.py."""
    if not stored or not stored.startswith(SECRET_SETTING_PREFIX):
        return stored or ""
    try:
        return fernet.decrypt(stored[len(SECRET_SETTING_PREFIX):].encode()).decode()
    except Exception:
        # Тот же выбор, что и на бэкенде: нерасшифровываемое значение — не
        # plaintext-токен, слать его в Telegram API нельзя. Оповещения
        # отключатся, о причине скажет лог.
        logger.error("не удалось расшифровать секрет настроек")
        return ""


class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String)


def refresh_config():
    """Подтягивает настройки из БД в CONFIG (вызывается периодически из manager)."""
    try:
        with Session() as s:
            for row in s.execute(select(Setting)).scalars().all():
                caster = _CONFIG_TYPES.get(row.key)
                if caster is None:
                    continue  # ключ не влияет на воркер (например, performance_profile)
                value = _decrypt_setting(row.value) if row.key in SECRET_SETTING_KEYS else row.value
                try:
                    CONFIG[row.key] = caster(value) if value != "" else ("" if caster is str else CONFIG[row.key])
                except (TypeError, ValueError):
                    pass
    except Exception:
        logger.error("не удалось прочитать настройки", exc_info=True)


def _alert_cooldown_passed(person_id: int) -> bool:
    """True, если по этой персоне можно слать алерт (кулдаун истёк).

    Кулдаун общий на все каналы (§6: Telegram, email, звук) и берётся ОДИН
    раз на событие, а не каждым каналом по отдельности: иначе первый канал
    забирал бы ключ, а второй молча пропускал бы каждый алерт — почта не
    работала бы вовсе при включённом Telegram. Недоступность Redis не должна
    глушить оповещения, поэтому ошибка трактуется как «кулдаун истёк».
    """
    try:
        return r.set(f"alert_cooldown:{person_id}", "1",
                     ex=CONFIG["alert_cooldown_sec"], nx=True) is not None
    except Exception:
        return True


def send_person_alert(person_id: int, name: str, camera_id: int, snapshot_path: str):
    """Watchlist-оповещение по всем настроенным каналам (SPEC §6).

    Вызывается из фоновой нити: и Telegram, и SMTP — сетевые вызовы до
    нескольких секунд, в нити обработки кадров они срезали бы FPS детекции.
    Отказ одного канала не отменяет другой — они настраиваются независимо, и
    администратор, включивший оба, ждёт дублирования, а не «какого-нибудь».
    """
    if not _alert_cooldown_passed(person_id):
        return
    text_msg = f"⚠️ FaceWatch: обнаружена персона «{name}» на камере #{camera_id}"
    send_telegram_alert(text_msg, person_id, camera_id)
    send_email_alert("FaceWatch: обнаружена персона из watchlist", text_msg,
                     person_id, camera_id)


def send_email_alert(subject: str, text_msg: str, person_id: int | None = None,
                     camera_id: int | None = None):
    """Письмо по SMTP-настройкам из БД. Молчит, если почта не настроена."""
    try:
        sent = mailer.send_email(
            CONFIG["smtp_host"], int(CONFIG["smtp_port"]), CONFIG["smtp_user"],
            CONFIG["smtp_password"], CONFIG["smtp_tls"], CONFIG["smtp_from"],
            CONFIG["alert_email_to"], subject, text_msg,
        )
    except mailer.MailerError as e:
        # Отдельный лог от Telegram: администратор должен видеть, какой
        # именно канал молчит. Текст MailerError не содержит пароля.
        logger.warning("ошибка отправки почтового уведомления",
                       extra={"person_id": person_id, "camera_id": camera_id,
                              "error": str(e)})
        return False
    except Exception:
        logger.warning("ошибка отправки почтового уведомления", exc_info=True,
                       extra={"person_id": person_id, "camera_id": camera_id})
        return False
    return sent


def send_telegram_alert(text_msg: str, person_id: int | None = None,
                        camera_id: int | None = None):
    """Telegram-оповещение. Кулдаун снимается вызывающим (send_person_alert)."""
    token = CONFIG["telegram_bot_token"]
    chat = CONFIG["telegram_chat_id"]
    if not token or not chat:
        return False
    try:
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"chat_id": chat, "text": text_msg}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=data, method="POST",
        )
        urllib.request.urlopen(req, timeout=5).read()
    except Exception:
        logger.warning("ошибка отправки Telegram-уведомления", exc_info=True, extra={"person_id": person_id, "camera_id": camera_id})
        return False
    return True


class Camera(Base):
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    rtsp_url_enc = Column(Text)
    sub_rtsp_url_enc = Column(Text)      # субпоток для аналитики (ТЗ 18.1)
    location = Column(String)
    enabled = Column(Boolean)
    # SPEC §2: record_only (по умолчанию) | analytics. Слой записи берёт все
    # включённые камеры, слой аналитики — только analytics.
    mode = Column(String, default="record_only")
    status = Column(String)
    roi = Column(JSON)
    motion_sensitivity = Column(Integer)
    onvif_enabled = Column(Boolean, default=False)     # ТЗ 18.7
    onvif_host = Column(String)
    onvif_port = Column(Integer)
    onvif_username = Column(String)
    onvif_password_enc = Column(Text)
    # SPEC §5: собственная глубина хранения камеры; NULL — следовать за
    # глобальной настройкой (см. models.py бэкенда).
    retention_days = Column(Integer)
    # SPEC §6: расписание детекции (день/ночь, рабочие часы). NULL — детекция
    # круглосуточно; см. detection_schedule.py о формате и о том, почему
    # «пусто» это «всегда», а не «никогда».
    detection_schedule = Column(JSON)
    # SPEC §6: «запись только при движении (опционально)». Флаг не касается
    # слоя записи (§2), он включает досрочную уборку сегментов без
    # движения — см. motion_windows.py и prune_motionless_segments().
    record_on_motion = Column(Boolean, default=False)


class MotionWindow(Base):
    """Промежуток наблюдения камеры слоем аналитики (SPEC §6).

    Схему создаёт бэкенд (`models.MotionWindow`); здесь объявлены только
    те колонки, которые нужны воркеру на запись и на уборку.
    """

    __tablename__ = "motion_windows"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"))
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    motion = Column(Boolean, default=False)


class Person(Base):
    __tablename__ = "persons"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    status = Column(String)
    avatar_path = Column(String)
    centroid = Column(Vector(512))
    alert_on_detection = Column(Boolean, default=False)
    # SPEC §15: теги персоны. Схему заводит бэкенд (миграции в main.py),
    # воркер только читает их на публикации события. Но объявить колонку
    # без умолчания нельзя: SQLAlchemy подставляет в INSERT явный NULL для
    # колонок без default, а колонка NOT NULL — то есть заведение новой
    # персоны падало бы NotNullViolation на каждом незнакомом лице.
    # server_default повторяет схему бэкенда, default=list закрывает путь
    # ORM. (Тот же класс, что находка цикла 5 про alert_on_detection.)
    tags = Column(ARRAY(Text), nullable=False,
                  server_default=text("'{}'::text[]"), default=list)
    created_at = Column(DateTime)


class FaceEvent(Base):
    __tablename__ = "face_events"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"))
    person_id = Column(Integer, ForeignKey("persons.id", ondelete="SET NULL"), nullable=True)
    ts = Column(DateTime)
    snapshot_path = Column(String)
    orig_snapshot_path = Column(String)
    enhanced = Column(Boolean, default=False)
    embedding = Column(Vector(512))
    bbox = Column(JSON)
    is_known = Column(Boolean)


class VideoSegment(Base):
    """Копия модели бэкенда; схему создаёт он (`models.VideoSegment`).

    `ondelete` — правило DDL, и в бою его ставит бэкенд, поэтому на работу
    воркера эти слова не влияют вовсе. Стоят они здесь ради тестов: набор,
    создающий таблицы из **этих** моделей, иначе получает схему без
    каскада, то есть проверяет систему, которой не существует. Ровно на
    этом цикл 53 потерял бы находку второй раз — первый был в
    `tests/test_segment_index.py`, где своя модель обходилась без внешнего
    ключа совсем (см. её шапку).
    """

    __tablename__ = "video_segments"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id", ondelete="CASCADE"))
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    file_path = Column(String)
    event_type = Column(String)
    duration_sec = Column(Integer)
    # SPEC §21: фактический расход и выбор жертв циклической перезаписи.
    size_bytes = Column(BigInteger, default=0)


def detect_providers() -> list[str]:
    """Автоопределение аппаратного ускорения (ТЗ 18.2).
    Порядок предпочтения: CUDA → DirectML (Windows/AMD) → OpenVINO (Intel) → CPU."""
    try:
        import onnxruntime as ort
        available = set(ort.get_available_providers())
    except Exception:
        return ["CPUExecutionProvider"]
    preferred = [
        "CUDAExecutionProvider",
        "DmlExecutionProvider",
        "OpenVINOExecutionProvider",
        "CPUExecutionProvider",
    ]
    chosen = [p for p in preferred if p in available]
    return chosen or ["CPUExecutionProvider"]


ACCELERATOR = "CPU"
# Глобальная ссылка на модель: нити камер читают её каждый кадр, поэтому
# смена модели в профиле применяется без перезапуска контейнера.
FACE_APP = None

# Отметки живости менеджера (см. liveness.py). Заводятся в manager(); None
# означает «менеджер ещё не стартовал» — в этом состоянии `/health` не
# может судить о зависании и не судит.
HEARTBEAT = None


# Модули insightface, которые системе действительно нужны.
#
# FaceAnalysis по умолчанию поднимает ПЯТЬ моделей: detection,
# landmark_3d_68, landmark_2d_106, genderage, recognition — и прогоняет
# последние четыре НА КАЖДОЕ ЛИЦО КАЖДОГО кадра. FaceWatch из результата
# читает ровно два поля: `bbox` (детекция) и `normed_embedding`
# (распознавание); возраст, пол и лицевые точки не использует нигде — ни
# в событиях, ни в поиске, ни в кластеризации.
#
# Замерено на цепочке §19 (buffalo_s, det_size 640, один поток, кадр с
# шестью лицами): 723 мс/кадр со всеми модулями против 112 мс/кадр с
# этими двумя — **6.5×**, при побитово том же наборе bbox и эмбеддингов.
# Это самый дорогой шаг слоя аналитики, и три четверти его стоимости
# уходили на данные, которые никто не читает.
FACE_MODULES = ["detection", "recognition"]


def analytics_threads() -> int:
    """Потоков ORT на камеру analytics (SPEC §16, §19).

    Считается из числа ядер машины и разрешённого числа камер analytics —
    см. ort_threads.py о том, почему без этого потолка одна камера
    занимала машину целиком, мешала слою записи (§2) и на четырёх камерах
    роняла канал ниже норматива §19 «≥ 5 FPS».
    """
    return analytics_thread_budget(
        os.cpu_count() or 1,
        int(CONFIG["analytics_cameras_max"]),
        int(CONFIG["analytics_threads"]),
    )


def load_face_app(model_name: str | None = None):
    """Загружает модель детекции/распознавания с учётом профиля (ТЗ 18.5)."""
    global ACCELERATOR, FACE_APP
    # Потолок CPU ставится ДО создания сессий: опции читаются в момент
    # создания, у существующей сессии пул уже свой.
    threads = analytics_threads()
    limited = limit_ort_threads(threads)
    from insightface.app import FaceAnalysis
    name = model_name or CONFIG["face_model"]
    providers = detect_providers()
    ACCELERATOR = providers[0].replace("ExecutionProvider", "")
    size = int(CONFIG["detect_width"])
    logger.info("модель загружена", extra={"model": name, "accelerator": ACCELERATOR,
                                           "det_size": size, "ort_threads": threads,
                                           "ort_threads_applied": limited})
    app = FaceAnalysis(name=name, providers=providers, allowed_modules=FACE_MODULES)
    app.prepare(ctx_id=0, det_size=(size, size))
    FACE_APP = app
    return app


def _try_load_model() -> bool:
    """Загружает модель, не роняя процесс при отказе (SPEC §2).

    Возвращает успех и оставляет причину отказа в `MODEL_ERROR`, чтобы
    интерфейс мог показать её администратору: молчаливое «аналитика не
    работает» отличить от «камеры нет в кадре» невозможно.
    """
    global MODEL_ERROR
    logger.info("загрузка модели InsightFace...")
    try:
        load_face_app()
    except Exception as exc:
        MODEL_ERROR = f"{type(exc).__name__}: {exc}"[:300]
        logger.error(
            "не удалось загрузить модель распознавания — слой аналитики "
            "выключен, запись при этом продолжается (SPEC §2). При первом "
            "запуске модель скачивается из интернета; на изолированном "
            "сервере положите её в том insightface-models вручную",
            exc_info=True, extra={"model": CONFIG["face_model"]},
        )
        return False
    MODEL_ERROR = None
    logger.info("модель готова")
    return True


# Причина, по которой модель не загрузилась (None — загружена). Публикуется
# в Redis вместе со статусом слоя записи, чтобы интерфейс объяснял отказ
# аналитики, а не показывал пустую стену распознавания без пояснений.
MODEL_ERROR: str | None = None

# Как часто пробовать загрузить модель заново после отказа. Модель может
# появиться без перезапуска контейнера — например, администратор положил
# файлы в том, — и требовать ради этого рестарта незачем.
MODEL_RETRY_SEC = 300

_last_status: dict[int, str] = {}

# Камеры, чей статус в этот момент известен слою записи. Для них слой
# записи — источник истины, а нить аналитики свой вердикт не навязывает.
#
# Зачем разделение владения. `Camera.status` решает, покажет ли интерфейс
# live-картинку, а картинка идёт по HLS из MediaMTX — значит статус обязан
# отражать состояние потока ИМЕННО в слое записи. Слой записи к тому же
# тянет все 120 камер (SPEC §2), а слой аналитики знает лишь про свои 2.
#
# Без разделения два писателя дерутся: нить аналитики зовёт
# `update_status("online")` на каждом кадре, слой записи раз в ~10 с
# ставил бы `offline`, и статус мигал бы с записью в БД и публикацией в
# Redis на каждом обороте.
#
# Когда Control API молчит, камера попадает в `unknown`, из этого набора
# выбывает — и вердикт нити аналитики снова в силе. То есть слой аналитики
# остаётся резервным источником ровно на случай недоступного MediaMTX.
_record_layer_owned: set[int] = set()


def update_status(cam_id: int, status: str, *, source: str = "analytics"):
    if source == "analytics" and cam_id in _record_layer_owned:
        return
    # Пишем в БД/паблишим только при фактической смене статуса,
    # иначе офлайн-камера спамит запись каждые 2 секунды.
    if _last_status.get(cam_id) == status:
        return
    _last_status[cam_id] = status
    with Session() as s:
        cam = s.get(Camera, cam_id)
        if cam:
            cam.status = status
            s.commit()
    try:
        r.publish("cameras:status", json.dumps({"camera_id": cam_id, "status": status}))
    except Exception:
        pass


_DECODE_ACCEL_LOGGED = False

# ТЗ 12: "детект зависших потоков". Без этих свойств ни cap.open(), ни
# cap.read() не имеют тайм-аута на FFMPEG-бэкенде OpenCV — классическая
# поломка RTSP, когда TCP-сессия остаётся формально открытой (NAT-keepalive,
# зависшая прошивка камеры), приводит к тому, что демультиплексор ffmpeg
# блокируется внутри cap.read() на неопределённое время вместо того, чтобы
# вернуть ошибку. В этом случае существующий backoff-реконнект (см. цикл
# reconnect_attempt в camera_worker()) никогда не срабатывает — нить
# считается «живой» (заблокирована ≠ мертва), manager() её не перезапускает,
# а на shutdown join(timeout=...) просто истекает, не освобождая ресурс.
# Значения — компромисс между устойчивостью к обычным сетевым паузам
# (слишком короткий тайм-аут даёт ложные переподключения при кратковременных
# заторах) и временем восстановления (ТЗ: RTO ≤ 5 минут для всей системы,
# здесь — на один поток из до 16).
# Как часто спящая по расписанию камера проверяет, не открылось ли окно.
# 30 с: граница окна размывается максимум на полминуты (для «рабочих часов»
# это незаметно), а спящая камера при этом ходит в БД вдвое реже, чем
# работающая (load_cam_state раз в 10 с), — на 120 камерах, стоящих ночью,
# разница заметна.
SCHEDULE_POLL_SEC = 30.0

RTSP_OPEN_TIMEOUT_MSEC = 10_000
RTSP_READ_TIMEOUT_MSEC = 15_000


def open_capture(url: str) -> cv2.VideoCapture:
    """Открывает RTSP-поток аналитики с попыткой аппаратного декодирования
    (ТЗ 18.2, вторая половина — инференс уже автоопределялся, декодирование
    кадров нет). `VIDEO_ACCELERATION_ANY` заставляет ffmpeg-бэкенд OpenCV
    попробовать доступный HW-ускоритель (VAAPI/QuickSync/NVDEC) и прозрачно
    откатиться на программное декодирование, если ничего не найдено — то же
    поведение и в песочнице без GPU, и на целевом N100 без настроенного
    VAAPI, поэтому безопасно включать по умолчанию.
    Свойства должны быть выставлены ДО open() — после открытия потока
    OpenCV их уже не применяет."""
    global _DECODE_ACCEL_LOGGED
    cap = cv2.VideoCapture()
    try:
        cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, RTSP_OPEN_TIMEOUT_MSEC)
        cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, RTSP_READ_TIMEOUT_MSEC)
    except Exception:
        pass  # сборка OpenCV без поддержки свойства — откат на прежнее поведение без тайм-аута
    if hw_decode_requested():
        try:
            cap.set(cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY)
        except Exception:
            pass  # сборка OpenCV без поддержки свойства — не критично, откат на софт-декод
    if not _DECODE_ACCEL_LOGGED:
        _DECODE_ACCEL_LOGGED = True
        logger.info("декодирование видео", extra={"hw_accelerator": detect_hw_accelerator_name()})
    cap.open(url, cv2.CAP_FFMPEG)
    return cap


def capture_resolution(cap) -> tuple[int | None, int | None]:
    """Фактическое разрешение открытого захвата, либо (None, None).

    Сначала свойства захвата: они не стоят ни одного декодированного кадра.
    Свойство регулярно отдаёт 0 — сборка OpenCV без него, поток, у которого
    заголовки ещё не разобраны, — и тогда берётся первый кадр. Кадр честнее
    свойства (это ровно те пиксели, которые получит детектор), но стоит
    декодирования, поэтому он второй, а не первый.

    Прочитанный здесь кадр теряется для вызывающего. Это осознанно: замер
    делается один раз при открытии потока, а цикл кадров ниже читает
    свежие — на потоке в 10-15 fps потеря одного кадра при старте не
    значит ничего, и возвращать его наружу ради этого не стоит усложнения.

    Исключения гасятся: замер — вспомогательная операция, и падать на ней
    нельзя. Не измерилось — вызывающий получит (None, None) и оставит
    поток как есть (см. analytics_source.choose_analytics_source).
    """
    width = height = None
    try:
        width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
        height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
    except Exception:
        width = height = 0
    if width and height:
        return width, height
    try:
        ok, frame = cap.read()
        if ok and frame is not None:
            h, w = frame.shape[:2]
            return int(w), int(h)
    except Exception:
        pass
    return None, None


def publish_analytics_source(cam_id: int, decision: dict) -> None:
    """Выбранный поток аналитики → Redis, для §9-мониторинга.

    §9 требует показывать статус потоков; до этого момента дежурный видел
    по камере аналитики только FPS детекции и не мог отличить «детекция
    идёт по субпотоку, как задумано» от «субпоток оказался ниже порога §15
    и аналитика молча переехала на основной поток» — а это разная нагрузка
    на сервер и разное качество распознавания.
    """
    try:
        r.hset("worker:analytics_source", str(cam_id), json.dumps({
            "stream": decision.get("stream"),
            "reason": decision.get("reason"),
            "width": decision.get("width"),
            "height": decision.get("height"),
            "note": describe(decision),
        }, ensure_ascii=False))
    except Exception:
        pass  # мониторинг не должен ронять аналитику


def drop_analytics_source(cam_id: int) -> None:
    """Снять запись о потоке аналитики (нить встала или не поднялась).

    Запись описывает живую аналитику. Пережив нить, она превращается в
    утверждение о том, чего нет: панель §9 показывала бы «Субпоток
    704×576» на камере, которая сейчас не обрабатывается вовсе, — тот же
    класс, что «честный ноль вместо последнего FPS» в цикле паузы ниже.
    """
    try:
        r.hdel("worker:analytics_source", str(cam_id))
    except Exception:
        pass


def open_analytics_capture(cam_id: int, rtsp_url: str, sub_rtsp_url: str | None):
    """Открывает поток аналитики, соблюдая порог §2/§15 по разрешению.

    Возвращает (cap, decision). `cap` может быть закрытым — вызывающий
    проверяет isOpened() и уходит на обычный backoff, как и раньше.

    Порядок именно такой: сначала открыть предпочтительный поток
    (субпоток, если задан), измерить его, и только по измеренному числу
    решать. Спросить разрешение, не открыв поток, нельзя — профиль ONVIF
    для этого не годится (прошивки врут, а §2 говорит о разрешении
    приходящего кадра), поэтому лишнее открытие субпотока здесь
    неизбежно. Стоит оно одного соединения на старте нити.
    """
    preferred = sub_rtsp_url or rtsp_url
    cap = open_capture(preferred)
    if not cap.isOpened():
        # Не открылось — решать не по чему. Отдаём как есть: вызывающий
        # уйдёт на backoff и попробует снова, и замер случится тогда.
        return cap, choose_analytics_source(rtsp_url, sub_rtsp_url)

    width, height = capture_resolution(cap) if sub_rtsp_url else (None, None)
    decision = choose_analytics_source(rtsp_url, sub_rtsp_url, width, height)

    if decision["url"] != preferred:
        # Порог не выдержан — переоткрываемся на основном потоке.
        # warning, а не info: это ухудшение против задуманной схемы (декод
        # основного потока стоит кратно дороже субпотока), и дежурный
        # должен узнать о нём из журнала, а не догадаться по загрузке CPU.
        logger.warning("субпоток ниже порога §15, аналитика переведена на основной поток",
                       extra={"camera_id": cam_id, "sub_width": width, "sub_height": height,
                              "min_width": 640, "min_height": 480})
        cap.release()
        cap = open_capture(decision["url"])
    elif decision["reason"] == SUB_BELOW_FLOOR_NO_MAIN:
        logger.warning("субпоток ниже порога §15, но переключиться некуда",
                       extra={"camera_id": cam_id, "sub_width": width, "sub_height": height})

    return cap, decision


def build_roi_mask(roi: dict | None, shape) -> np.ndarray | None:
    """Полигоны хранятся в нормализованных координатах [0..1]."""
    if not roi or not roi.get("polygons"):
        return None
    h, w = shape[:2]
    mask = np.zeros((h, w), dtype=np.uint8)
    for poly in roi["polygons"]:
        pts = np.array([[p[0] * w, p[1] * h] for p in poly], dtype=np.int32)
        if pts.shape[0] >= 3:
            cv2.fillPoly(mask, [pts], 255)
    return mask


def bbox_in_roi(bbox, mask: np.ndarray | None) -> bool:
    if mask is None:
        return True
    x1, y1, x2, y2 = [int(v) for v in bbox]
    cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
    if 0 <= cy < mask.shape[0] and 0 <= cx < mask.shape[1]:
        return bool(mask[cy, cx])
    return False


# Цикл 16 (было известным пробелом с цикла 15, см. REVIEW_LOG.md):
# SELECT ближайшего центроида → сравнение с порогом → INSERT в
# find_or_create_person() ниже не были атомарны. До 16 нитей camera_worker()
# (по одной на камеру) делят одну БД — если две камеры видят одного и того
# же неизвестного человека в одном узком временном окне, обе могут пройти
# SELECT до того, как другая сделает INSERT, и каждая решит «совпадения
# нет» — на выходе два разных Person для одного физического человека
# (классический TOCTOU). Питоновский Lock() эту гонку не закрыл бы — нити
# разных camera_worker() внутри одного процесса он бы сериализовал, но
# несколько процессов воркера с общей БД снова гонялись бы. Транзакционный
# advisory lock Postgres сериализует блок между всеми процессами и нитями,
# которые используют эту БД, и снимается автоматически на ближайшем
# commit()/rollback() текущей транзакции — держать его руками не нужно.
PERSON_DEDUP_LOCK_KEY = 0x46575044  # 'FWPD' (FaceWatch Person Dedup) как bigint-ключ


def find_or_create_person(s, emb: np.ndarray) -> tuple[int, bool]:
    # Держит лок до commit()/rollback() вызывающей стороны — в process_faces()
    # это s.commit() сразу после вызова, так что окно блокировки — одна
    # SELECT + опциональный INSERT, не весь цикл обработки кадра.
    s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": PERSON_DEDUP_LOCK_KEY})
    res = s.execute(text(
        "SELECT id, 1 - (centroid <=> CAST(:e AS vector)) AS sim FROM persons "
        "WHERE centroid IS NOT NULL ORDER BY centroid <=> CAST(:e AS vector) LIMIT 1"
    ), {"e": str(emb.tolist())}).first()
    if res and res.sim is not None and (1 - res.sim) < CONFIG["similarity_threshold"]:
        return res.id, True
    p = Person(name="", status="unknown", centroid=emb.tolist(), created_at=datetime.utcnow())
    s.add(p)
    s.flush()
    return p.id, False


def save_face_snapshot(frame, cam_id: int, bbox) -> str | None:
    """Кроп лица с запасом по краям (вплотную по bbox лицо выглядит обрезанным).
    Возвращает None, если записать файл не удалось."""
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = [int(v) for v in bbox]
    pad_x = int((x2 - x1) * 0.25)
    pad_y = int((y2 - y1) * 0.25)
    x1, y1 = max(0, x1 - pad_x), max(0, y1 - pad_y)
    x2, y2 = min(w, x2 + pad_x), min(h, y2 + pad_y)
    crop = frame[y1:y2, x1:x2] if (x2 > x1 and y2 > y1) else frame
    if crop.size == 0:
        crop = frame

    # Суффикс обязателен, а не косметика: имени из одной миллисекунды не
    # хватает. Все лица одного кадра режутся подряд, между ними нет ни
    # запроса к БД, ни сети, — два кропа укладываются в одну миллисекунду
    # штатно, и тогда второй cv2.imwrite молча затирал первый. Оба события
    # оставались в БД, но ссылались на один файл: в карточке одного
    # человека (и в его аватаре) оказывалось лицо другого. Отдельные
    # камеры не конфликтовали и раньше — cam_id в имени, — а вот лица
    # внутри кадра конфликтовали.
    fname = f"cam{cam_id}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}.jpg"
    fpath = os.path.join(MEDIA_PATH, "snapshots", fname)
    if not cv2.imwrite(fpath, crop, [cv2.IMWRITE_JPEG_QUALITY, 85]):
        logger.warning("не удалось записать снимок", extra={"camera_id": cam_id, "path": fpath})
        return None
    return f"snapshots/{fname}"


def resolve_snapshot_url(onvif_config: dict | None) -> str | None:
    """Адрес JPEG-снимка основного потока камеры (ONVIF GetSnapshotUri).

    Вызывается один раз при старте камеры. Детекция идёт на субпотоке
    (640×360) — резать оттуда лицо значит получить кроп в несколько десятков
    пикселей, который не спасёт никакой апскейл: информации в исходнике
    просто нет. Постоянно декодировать основной поток ради снимков нельзя —
    на 16 камерах это ровно та нагрузка, которую раздел 18 ТЗ велит
    избегать. Снимок берётся одним HTTP-GET и только в момент события.
    """
    if not onvif_config or not onvif_config.get("host"):
        return None
    host = onvif_config["host"]
    port = onvif_config.get("port") or 80
    user = onvif_config.get("username")
    pw = onvif_config.get("password")
    try:
        profiles = onvif_client.get_profiles(host, port, user, pw)
    except onvif_client.OnvifError:
        logger.info("GetProfiles недоступен, снимки будут резаться из кадра аналитики",
                    extra={"onvif_host": host})
        return None
    main_profile, _ = onvif_client.select_stream_profiles(profiles)
    if not main_profile:
        return None
    url = onvif_client.get_snapshot_uri(host, port, main_profile["token"], user, pw)
    if not url:
        logger.info("камера не поддерживает GetSnapshotUri, снимки из кадра аналитики",
                    extra={"onvif_host": host})
    return url


def fetch_snapshot_frame(snapshot_url: str, timeout: float = 4.0):
    """Скачивает и декодирует один полноразмерный кадр с камеры.

    Само скачивание — в snapshot_http.fetch_snapshot_bytes: учётные данные
    приходят внутри URI (их туда подставляет inject_credentials, потому что
    ffmpeg и OpenCV читают их только из URL), а HTTP так не умеет —
    urlopen() принимает "user:pass@host" за имя хоста и падает на резолве.
    Там же они превращаются в нормальную Basic/Digest-аутентификацию.

    None при любой проблеме: снимок — улучшение качества кропа, а не
    обязательный шаг, и недоступная камера не должна ронять обработку
    события; отказ при этом виден в логе, а не глохнет молча.
    """
    data = fetch_snapshot_bytes(snapshot_url, timeout)
    if not data:
        return None
    try:
        buf = np.frombuffer(data, dtype=np.uint8)
        return cv2.imdecode(buf, cv2.IMREAD_COLOR)
    except Exception:
        logger.warning("не удалось декодировать снимок камеры", exc_info=True)
        return None


def save_latest_frame(frame, cam_id: int):
    fpath = os.path.join(MEDIA_PATH, "snapshots", f"cam{cam_id}_latest.jpg")
    cv2.imwrite(fpath, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])


def load_cam_state(cam_id: int) -> tuple[dict | None, bool, int | None, dict | None]:
    """(roi, active, motion_sensitivity, detection_schedule).

    `active=False` — нить аналитики должна завершиться: камера удалена,
    отключена **или переведена в режим record_only**. Последнее — то же
    самое условие остановки, что и отключение: переключение режима в
    админке обязано применяться без перезапуска слоёв (SPEC §2), а нить,
    продолжающая детекцию на камере, которую перевели «только на запись»,
    ровно этому и противоречит.
    """
    with Session() as s:
        cam = s.get(Camera, cam_id)
        if cam is None or not cam.enabled:
            return None, False, None, None
        if (getattr(cam, "mode", None) or "record_only") != "analytics":
            return None, False, None, None
        return (cam.roi, True, getattr(cam, "motion_sensitivity", None),
                getattr(cam, "detection_schedule", None))


# ТЗ 18.7: события движения/присутствия от ONVIF-камеры вместо постоянного
# MOG2-префильтра на CPU. cam_id -> время последнего события/последнего
# успешного pull'а — camera_worker считает ONVIF "здоровым" (и пропускает
# MOG2 целиком ради экономии CPU) только пока последний успешный pull был
# недавно; при сетевых проблемах/отвале подписки автоматически откатывается
# на обычный MOG2-префильтр, не теряя детекцию совсем.
ONVIF_LAST_MOTION: dict[int, float] = {}
ONVIF_LAST_HEALTHY: dict[int, float] = {}
ONVIF_HEALTHY_WINDOW_SEC = 30.0
ONVIF_MOTION_FRESH_SEC = 5.0


def onvif_healthy(cam_id: int) -> bool:
    last = ONVIF_LAST_HEALTHY.get(cam_id, 0.0)
    return (time.time() - last) < ONVIF_HEALTHY_WINDOW_SEC


def onvif_motion_recent(cam_id: int) -> bool:
    last = ONVIF_LAST_MOTION.get(cam_id, 0.0)
    return (time.time() - last) < ONVIF_MOTION_FRESH_SEC


def _flush_motion_windows(cam_id: int, windows) -> None:
    """Записать закрывшиеся окна наблюдения камеры (SPEC §6).

    Вызывается из цикла кадров, поэтому обязана быть немой: отказ БД не
    должен уносить нить камеры (цикл 23 — обращения к БД в цикле кадров
    как источник падений). Потерянное окно — это дыра в покрытии, то есть
    сегменты за этот промежуток просто останутся в архиве; ошибка в
    безопасную сторону, в отличие от остановки детекции.
    """
    if not windows:
        return
    try:
        with Session() as s:
            for w in windows:
                s.add(MotionWindow(
                    camera_id=cam_id,
                    started_at=datetime.utcfromtimestamp(w.started_ts),
                    ended_at=datetime.utcfromtimestamp(w.ended_ts),
                    motion=bool(w.motion),
                ))
            s.commit()
    except Exception:
        logger.warning("не удалось записать окно наблюдения движения",
                       exc_info=True, extra={"camera_id": cam_id})


def onvif_poll_worker(cam_id: int, host: str, port: int, username: str | None, password: str | None,
                      stop_event: threading.Event | None = None):
    """Фоновая нить на камеру с onvif_enabled: держит PullPoint-подписку и
    складывает события движения в ONVIF_LAST_MOTION/ONVIF_LAST_HEALTHY.
    Никогда не поднимает исключение наружу — ошибки только логируются и
    ведут к переподписке с экспоненциальной задержкой (тот же backoff, что
    и у RTSP-реконнекта).

    `stop_event` — персональный сигнал остановки от породившего
    `camera_worker()`; нить живёт ровно столько же, сколько он. Глобального
    `shutdown_event` для этого недостаточно: `camera_worker()` завершается и
    поодиночке (камеру выключили/удалили в админке, необработанное
    исключение в цикле кадров), и без личного сигнала такая нить оставалась
    бы висеть с PullPoint-подпиской и сокетом до остановки всего процесса,
    а manager() через ~10 с поднимал бы рядом ещё одну (цикл 23; тот же
    класс утечки, что закрыт циклом 16 для случая «RTSP не открывается»).
    """
    attempt = 0

    def _stopping(timeout: float = 0.0) -> bool:
        """Ждёт `timeout` и говорит, пора ли останавливаться.

        Ждём именно на личном событии — оно ставится и при глобальном
        shutdown (см. camera_worker), поэтому реакция на общую остановку не
        замедляется.
        """
        if stop_event is not None:
            return stop_event.wait(timeout) or shutdown_event.is_set()
        return shutdown_event.wait(timeout)

    while not _stopping():
        try:
            subscription_url = onvif_client.create_pull_point_subscription(host, port, username, password)
            logger.info("ONVIF-подписка создана", extra={"camera_id": cam_id, "onvif_host": host})
            attempt = 0
            while not _stopping():
                try:
                    events = onvif_client.pull_messages(subscription_url, username, password)
                except onvif_client.OnvifError:
                    logger.warning("ONVIF pull не удался, переподписка", exc_info=True, extra={"camera_id": cam_id})
                    break
                ONVIF_LAST_HEALTHY[cam_id] = time.time()
                for ev in events:
                    if onvif_client.is_motion_event(ev.get("topic"), ev.get("state")):
                        ONVIF_LAST_MOTION[cam_id] = time.time()
                if _stopping(0.5):
                    return
        except onvif_client.OnvifError:
            delay = reconnect_delay(attempt)
            logger.warning(
                "не удалось создать ONVIF-подписку, повтор",
                exc_info=True, extra={"camera_id": cam_id, "retry_in_sec": round(delay, 1)},
            )
            attempt += 1
            if _stopping(delay):
                return
        except Exception:
            # Не даём непредвиденной ошибке ONVIF уронить всю нить — это
            # вспомогательный источник детекции, MOG2-фолбэк в camera_worker
            # продолжает работать независимо от состояния этой нити.
            logger.error("непредвиденная ошибка ONVIF-нити", exc_info=True, extra={"camera_id": cam_id})
            if _stopping(reconnect_delay(attempt)):
                return
            attempt += 1


def camera_worker(cam_id: int, rtsp_url: str, face_app, sub_rtsp_url: str | None = None,
                   onvif_config: dict | None = None, cpus: list[int] | None = None):
    """Аналитика по одной камере (SPEC §6).

    Основной поток эта функция больше не трогает вообще: его тянет и пишет
    MediaMTX (слой записи, см. record_layer.py). Здесь открывается только
    поток аналитики — субпоток, если он задан, иначе основной, — и нить
    занимается исключительно детекцией и распознаванием. Так выполняется
    §2: «Отказ аналитики НЕ влияет на запись» — падение или перезапуск этой
    нити ничего не делает с архивом.

    `cpus` — ядра NUMA-ноды, отведённой этому каналу (SPEC §17). None или
    пустой список означает «не привязывать»: односокетная машина или
    привязка выключена оператором.
    """
    # Привязка — ПЕРВЫМ действием нити, до открытия захвата и до первого
    # кадра. Порядок здесь не косметический: политика памяти Linux —
    # first-touch, страница достаётся ноде того потока, который к ней
    # обратился первым. Привязка после первых кадров закрепила бы за
    # каналом ядра одной ноды и буферы другой — то есть ровно тот случай,
    # который §17 просит устранить, только теперь уже намертво.
    if cpus:
        pinned = cpu_affinity.pin_current_thread(cpus)
        logger.info("канал привязан к ядрам", extra={
            "camera_id": cam_id, "cpus": sorted(cpus), "pinned": pinned})
    # Снимки лиц режутся из полноразмерного кадра, который камера отдаёт по
    # HTTP (ONVIF GetSnapshotUri), а не из кадра аналитики: на субпотоке
    # 640×360 лицо занимает несколько десятков пикселей. Адрес резолвится
    # один раз при старте; None означает откат на кроп из кадра аналитики.
    snapshot_url = resolve_snapshot_url(onvif_config)
    # Поток аналитики выбирается по фактически измеренному разрешению, а не
    # по наличию субпотока: §2 и §15 называют допустимым источником кадров
    # основной поток либо субпоток «с разрешением не ниже 640×480», и
    # субпоток ниже порога в этот список не входит. Решение принимается
    # один раз при старте нити и живёт в analyze_url — все переоткрытия
    # ниже (реконнект, возврат из окна расписания) идут по нему же, поэтому
    # камера, уведённая с проваленного субпотока, там и остаётся.
    cap, source = open_analytics_capture(cam_id, rtsp_url, sub_rtsp_url)
    analyze_url = source["url"]
    logger.info("старт камеры", extra={
        "camera_id": cam_id,
        "analytics_stream": source["stream"],
        "analytics_source_reason": source["reason"],
        "analytics_width": source["width"],
        "analytics_height": source["height"],
        "hires_snapshots": bool(snapshot_url),
    })
    if not cap.isOpened():
        logger.error("не удалось открыть RTSP", extra={"camera_id": cam_id})
        update_status(cam_id, "offline")
        # Этот выход идёт МИМО общего finally ниже (он ещё не начался), а
        # manager() поднимает нить заново каждые ~10 с. Без снятия записи
        # камера с неоткрывающимся потоком навсегда осталась бы в §9 с тем
        # потоком, который у неё был в прошлый удачный запуск.
        drop_analytics_source(cam_id)
        return
    update_status(cam_id, "online")
    # Публикуется только для работающей нити: решение, принятое без
    # единого прочитанного кадра, описывает не аналитику, а намерение.
    publish_analytics_source(cam_id, source)

    # Запускается только после успешного открытия потока аналитики, а не
    # безусловно при входе в функцию: onvif_poll_worker — daemon-нить без
    # собственного условия остановки, кроме глобального shutdown_event
    # (см. её докстринг), поэтому раньше она переживала любой ранний return
    # camera_worker() выше. Для камеры с валидным onvif_host, но постоянно
    # неоткрывающимся RTSP (частая реальная поломка — неверный URL/пароль
    # именно субпотока при рабочем основном потоке) manager() пересоздаёт
    # camera_worker() каждые ~10с (см. цикл ниже в manager()), и каждый
    # перезапуск плодил ещё одну независимую ONVIF-нить со своей PullPoint-
    # подпиской и сокетом — ни одна из них никогда не останавливалась,
    # утечка нитей неограниченно росла со временем и в итоге валила процесс
    # воркера целиком (все камеры, не только сбойную). ONVIF-события всё
    # равно потребляются только внутри цикла кадров ниже (onvif_healthy()/
    # onvif_motion_recent()), который не выполняется без открытого cap —
    # переносить старт нити раньше этой точки не давало никакой пользы.
    # Персональный сигнал остановки ONVIF-нити: ставится в finally ниже, на
    # любом выходе из camera_worker() — штатном, по отключению камеры и по
    # необработанному исключению (цикл 23).
    onvif_stop = threading.Event()
    # SPEC §6: отметки «аналитика смотрела камеру и видела/не видела в ней
    # движение». Накапливаются в памяти нити и уходят в БД готовым окном
    # раз в минуту (motion_windows.py). Создаётся до try/finally, чтобы
    # незакрытое окно дописывалось и при аварийном выходе из нити.
    motion_tracker = MotionWindowTracker()
    if onvif_config and onvif_config.get("host"):
        threading.Thread(
            target=onvif_poll_worker,
            args=(cam_id, onvif_config["host"], onvif_config.get("port") or 80,
                  onvif_config.get("username"), onvif_config.get("password"), onvif_stop),
            daemon=True,
        ).start()

    try:
        bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
        last_proc = 0.0
        last_latest_save = 0.0
        last_state_reload = 0.0
        roi, active, motion_sens, det_schedule = load_cam_state(cam_id)
        roi_mask = None
        roi_mask_shape = None
        frame_counter = 0            # для пропуска кадров (ТЗ 18.3)
        fps_window_start = time.time()
        fps_frames = 0

        last_event_at: dict[int, float] = {}  # person_id -> время последнего события (тротлинг)
        last_motion = 0.0

        reconnect_attempt = 0
        # Освобождение ресурсов вынесено в finally ниже. Раньше оно стояло в
        # каждой из точек выхода по отдельности, и любое необработанное
        # исключение в цикле кадров уносило нить мимо них: RTSP-захват OpenCV
        # оставался открытым, а ffmpeg-репабликация — осиротевшим процессом
        # (Popen не убивается сборщиком мусора). manager() поднимал камеру
        # заново через ~10 с, поэтому утечка накапливалась по одному захвату и
        # одному ffmpeg на каждый сбой. Самый вероятный источник таких
        # исключений — не распознавание (оно и так под try), а обращения к БД
        # в цикле: load_cam_state() ходит в Postgres каждые 10 с, и обычная для
        # 24/7 перезагрузка БД роняла бы все 16 нитей разом (цикл 23).
        # SPEC §6: расписание детекции. Вне окна камера не обрабатывается
        # вовсе — захват отпускается, то есть уходит не только детектор, но
        # и декодирование. Это и есть смысл функции на целевом сервере: без
        # GPU декод идёт постоянно, независимо от частоты детекции
        # (cap.read() в цикле ниже не throttl'ится), и «детекция только
        # ночью» без освобождения захвата экономила бы гораздо меньше.
        #
        # Статус камеры при этом НЕ меняется. Соблазн выставить "offline"
        # велик, но §9 требует алерта на потерю потока — дежурный получал бы
        # его каждый вечер по расписанию. Камера в это время действительно
        # онлайн: слой записи (MediaMTX) продолжает писать её, пауза
        # касается только аналитики (§2 — слои независимы).
        paused = False
        while True:
            if shutdown_event.is_set():
                logger.info("остановка (shutdown), освобождаю ресурсы", extra={"camera_id": cam_id})
                update_status(cam_id, "offline")
                return

            if not schedule_active(det_schedule, datetime.now()):
                if not paused:
                    logger.info("вне окна расписания детекции, аналитика приостановлена",
                                extra={"camera_id": cam_id})
                    paused = True
                    if cap is not None:
                        cap.release()
                        cap = None
                    # Честный ноль в мониторинге вместо последнего значения:
                    # иначе панель показывала бы «5 FPS» на камере, которая
                    # сейчас не обрабатывается вообще.
                    try:
                        r.hset("worker:fps", str(cam_id), 0)
                    except Exception:
                        pass
                # Прерываемое ожидание: на shutdown уходим сразу, не досыпая.
                if shutdown_event.wait(SCHEDULE_POLL_SEC):
                    continue
                roi, active, motion_sens, det_schedule = load_cam_state(cam_id)
                roi_mask = None
                if not active:
                    logger.info("камера отключена, останавливаю обработку", extra={"camera_id": cam_id})
                    update_status(cam_id, "disabled")
                    return
                continue

            if paused or cap is None:
                logger.info("окно расписания открылось, возобновляю аналитику",
                            extra={"camera_id": cam_id})
                paused = False
                cap = open_capture(analyze_url)
                if not cap.isOpened():
                    # Тот же путь, что и при обычной потере потока ниже:
                    # ждём с backoff, а не крутим переоткрытие в цикле.
                    cap.release()
                    cap = None
                    if shutdown_event.wait(reconnect_delay(reconnect_attempt)):
                        continue
                    reconnect_attempt += 1
                    continue
                reconnect_attempt = 0

            ok, frame = cap.read()
            if not ok:
                update_status(cam_id, "offline")
                delay = reconnect_delay(reconnect_attempt)
                logger.warning(
                    "поток потерян, повтор подключения",
                    extra={"camera_id": cam_id, "retry_in_sec": round(delay, 1), "attempt": reconnect_attempt + 1},
                )
                # Прерываемое ожидание — при shutdown не держим камеру в сне
                # до 60с, а сразу уходим на освобождение ресурсов сверху цикла.
                if shutdown_event.wait(delay):
                    continue
                reconnect_attempt += 1
                cap.release()
                # Переоткрываем именно поток аналитики (субпоток, если задан) —
                # раньше здесь по ошибке использовался основной поток, из-за чего
                # детекция после первого разрыва связи молча переезжала на
                # основной поток в обход двухпоточной схемы (ТЗ 18.1).
                cap = open_capture(analyze_url)
                if cap.isOpened():
                    update_status(cam_id, "online")
                continue
            if reconnect_attempt:
                reconnect_attempt = 0

            now = time.time()
            # Адаптивная частота (ТЗ 18.3): при длительном покое опускаемся до idle_fps,
            # при первом же движении мгновенно возвращаемся к полной частоте.
            idle = (now - last_motion) > IDLE_AFTER_SEC
            target_fps = CONFIG["idle_fps"] if idle else CONFIG["detection_fps"]
            interval = 1.0 / max(1, target_fps)
            if now - last_proc < interval:
                continue
            last_proc = now

            # Пропуск кадров: анализируем каждый (frame_skip+1)-й отобранный кадр
            frame_counter += 1
            skip = int(CONFIG["frame_skip"])
            if skip and (frame_counter % (skip + 1)) != 0:
                continue

            # Фактический FPS детекции по камере → в Redis для мониторинга
            fps_frames += 1
            if now - fps_window_start >= 10.0:
                try:
                    r.hset("worker:fps", str(cam_id), round(fps_frames / (now - fps_window_start), 2))
                except Exception:
                    pass
                fps_window_start = now
                fps_frames = 0

            if now - last_latest_save > 2.0:
                save_latest_frame(frame, cam_id)
                last_latest_save = now

            if now - last_state_reload > 10.0:
                roi, active, motion_sens, det_schedule = load_cam_state(cam_id)
                roi_mask = None
                last_state_reload = now
                if not active:
                    # Камера отключена или удалена — корректно останавливаем
                    # поток. Ресурсы (захват, ffmpeg, ONVIF-нить) освобождает
                    # общий finally ниже; раньше этот путь глушил захват и
                    # ffmpeg, но оставлял ONVIF-нить висеть с подпиской, и
                    # повторное включение камеры добавляло рядом ещё одну.
                    logger.info("камера отключена, останавливаю обработку", extra={"camera_id": cam_id})
                    update_status(cam_id, "disabled")
                    return

            if roi_mask is None or roi_mask_shape != frame.shape[:2]:
                roi_mask = build_roi_mask(roi, frame.shape)
                roi_mask_shape = frame.shape[:2]

            # ТЗ 18.7: пока ONVIF-подписка камеры здорова (недавний успешный
            # pull), движение берётся из событий камеры — MOG2 целиком
            # пропускается ради экономии CPU ("камера делает предобработку на
            # своём чипе"). При проблемах с ONVIF (сеть, отвал подписки)
            # onvif_healthy() перестаёт быть True без дополнительной логики
            # здесь, и следующий же кадр прозрачно возвращается на обычный
            # MOG2-префильтр — детекция не останавливается.
            if onvif_config and onvif_healthy(cam_id):
                motion = onvif_motion_recent(cam_id)
            else:
                small = cv2.resize(frame, (640, 360))
                fg = bg.apply(small)
                if roi_mask is not None:
                    small_mask = cv2.resize(roi_mask, (640, 360), interpolation=cv2.INTER_NEAREST)
                    fg = cv2.bitwise_and(fg, fg, mask=small_mask)
                motion_pixels = int(np.count_nonzero(fg))
                # Чувствительность камеры переопределяет общий порог профиля
                threshold = motion_sens if motion_sens else CONFIG["motion_threshold"]
                motion = motion_pixels > threshold

            # Префильтр движения (ТЗ 18.4): в «максимальном» профиле отключается
            # и детектор лиц работает по каждому кадру.
            run_detector = motion or not CONFIG["motion_prefilter"]
            faces = []
            try:
                if run_detector:
                    # Читаем глобальную модель, чтобы подхватить её горячую замену
                    detected = (FACE_APP or face_app).get(frame)
                    faces = [f for f in detected if bbox_in_roi(f.bbox.tolist(), roi_mask)]
            except Exception:
                logger.error("ошибка распознавания", exc_info=True, extra={"camera_id": cam_id})
                faces = []

            # Движение больше не открывает сегмент: запись непрерывная и идёт
            # в MediaMTX (SPEC §5, §20). Здесь оно нужно только для
            # адаптивной частоты детекции — «мгновенный возврат к полной
            # частоте при появлении движения» (SPEC §19).
            if motion or faces:
                last_motion = now

            # SPEC §6: отметка наблюдения этого кадра. Лицо считается
            # движением независимо от префильтра: в «максимальном» профиле
            # MOG2 выключен вовсе (motion_prefilter=0), и без этого условия
            # сегмент с человеком в кадре, но без сработки префильтра,
            # уехал бы в удаление как «пустой».
            _flush_motion_windows(cam_id, motion_tracker.observe(now, motion or bool(faces)))

            if not faces:
                continue

            fh, fw = frame.shape[:2]
            try:
                process_faces(cam_id, frame, faces, fw, fh, now, last_event_at, snapshot_url)
            except Exception:
                # Любой сбой на одном кадре не должен убивать нить камеры
                logger.error("ошибка обработки лиц", exc_info=True, extra={"camera_id": cam_id})
    except Exception:
        # Нить камеры умирает — но в JSON-логе, а не молчаливым стектрейсом
        # threading.excepthook'а в stderr мимо ротации и структурированного
        # вывода (ТЗ 12). manager() поднимет камеру заново через ~10 с;
        # ресурсы к этому моменту уже освобождены блоком finally ниже.
        logger.error("нить камеры аварийно завершилась", exc_info=True,
                     extra={"camera_id": cam_id})
        update_status(cam_id, "offline")
    finally:
        # Единый finally на все пути выхода — штатный, по отключению камеры и
        # по необработанному исключению (цикл 23). Сначала снимаем ONVIF-нить
        # (она держит сокет и подписку на камере), затем отпускаем захват.
        # Сегмента и ffmpeg-репабликации здесь больше нет: с цикла 24 запись
        # ведёт MediaMTX, и остановка аналитики её не касается (SPEC §2).
        onvif_stop.set()
        # Незакрытое окно наблюдения — до минуты. Без этой строки каждый
        # перезапуск нити оставлял бы дыру в покрытии, а manager()
        # пересоздаёт нить каждые ~10 с при проблемах с потоком.
        _flush_motion_windows(cam_id, motion_tracker.close())
        # cap is None — камера остановлена в паузе по расписанию (§6),
        # захват уже отпущен.
        if cap is not None:
            cap.release()
        # Запись о выбранном потоке снимается вместе с нитью — см.
        # drop_analytics_source(). Панель §9 должна показать прочерк, а не
        # последнее известное значение.
        drop_analytics_source(cam_id)



class _PendingEvent:
    """Лицо, прошедшее cooldown, — всё нужное для события, снятое с ORM заранее.

    Существует ради того, чтобы фаза сети и инференса (снимок с камеры,
    прогон модели) шла вообще без открытой сессии SQLAlchemy: значения из
    `Person` читаются в фазе 1 и дальше живут обычными полями Python, а не
    ленивыми атрибутами, дёргающими БД в произвольный момент.
    """

    __slots__ = ("pid", "name", "is_known", "bbox", "bbox_json", "emb", "snap_rel",
                 "alert", "tags")

    def __init__(self, pid, name, is_known, bbox, bbox_json, emb, alert=False, tags=None):
        self.pid = pid
        self.name = name
        self.is_known = is_known
        self.bbox = bbox
        self.bbox_json = bbox_json
        self.emb = emb
        # Персона в watchlist (§6). Читается здесь же, в фазе 1, вместе с
        # остальными полями Person — после закрытия сессии `person` уже
        # недоступна, а флаг нужен на публикации, чтобы Стена подала звук.
        self.alert = alert
        # Теги персоны на момент детекции — Стена фильтрует ими живую
        # ленту (SPEC §15 «Фильтры и поиск по ленте»), а событие приходит
        # к ней по WebSocket, минуя /api/events.
        self.tags = list(tags or [])
        self.snap_rel = None


def process_faces(cam_id, frame, faces, fw, fh, now, last_event_at, snapshot_url=None):
    """Обработка лиц одного кадра: персоны → снимок → события.

    Разбита на три фазы, и границы между ними — не стилистика, а требование
    к 24/7-работе: **сессия БД не должна быть открыта во время сетевого
    запроса к камере и прогона модели.**

    Раньше всё это стояло внутри одного `with Session()`. Из-за
    `expire_on_commit=True` (умолчание sessionmaker) первое же обращение к
    `person.name` после `s.commit()` реактивировало объект и открывало
    новую транзакцию, которая жила до следующего коммита — то есть сквозь
    HTTP-запрос снимка (до 4 с таймаута) и прогон детектора по
    полноразмерному кадру. На 16 камерах это до 16 соединений в состоянии
    `idle in transaction` одновременно. До исчерпания пула (20+10) далеко,
    но длинные транзакции держат горизонт видимости и не дают autovacuum
    чистить мёртвые версии строк в `face_events` — таблице, куда пишется
    больше всего и которая чистится по расписанию хранения.

    Фаза 1 (БД) — персоны и cooldown. Атрибуты `Person` читаются **до**
    `s.commit()`, в уже открытой транзакции, поэтому реактивации не
    происходит вовсе.
    Фаза 2 (без БД) — снимок с камеры, детекция на нём, кропы. Самая
    долгая, и здесь соединение с БД не удерживается.
    Фаза 3 (БД) — вставка событий одной транзакцией.
    """
    # --- Фаза 1: БД. Персоны и cooldown. -------------------------------
    pending: list = []
    with Session() as s:
        for f in faces:
            emb = np.asarray(f.normed_embedding, dtype=np.float32)
            if emb.shape[0] != 512:
                continue
            bbox = f.bbox.tolist()
            pid, matched = find_or_create_person(s, emb)
            person = s.get(Person, pid)

            # Всё, что нужно дальше, снимается с ORM здесь — внутри
            # транзакции, которую откроет find_or_create_person, и до
            # коммита. Обращение к этим же атрибутам после commit() стоило
            # бы лишнего SELECT'а и, главное, новой транзакции.
            name = person.name or f"Неизвестный #{pid}"
            is_known = person.status == "known"
            wants_alert = bool(getattr(person, "alert_on_detection", False))
            # Теги (SPEC §15) снимаются здесь по той же причине, что и
            # остальные поля: после commit() объект отвязан, а обращение к
            # атрибуту стоило бы отдельного SELECT'а на каждое лицо кадра.
            tags = list(getattr(person, "tags", None) or [])
            s.commit()  # фиксируем возможную новую персону сразу

            bbox_json = {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]}

            # Watchlist-оповещение: свой redis-cooldown, шлём из фоновой нити,
            # чтобы сетевые вызовы каналов (§6: Telegram до 5 с, SMTP до 15 с)
            # не тормозили обработку кадров
            if is_known and wants_alert:
                threading.Thread(
                    target=send_person_alert,
                    args=(pid, name, cam_id, ""),
                    daemon=True,
                ).start()

            # Тротлинг событий: при 5 FPS человек в кадре генерировал бы
            # 18k событий/час (снимок + 4КБ вектор + задача апскейла на кадр).
            # Событие — не чаще раза в event_cooldown_sec на персону на камеру;
            # для live-оверлея каждый кадр уходит лёгкое сообщение type=box.
            if now - last_event_at.get(pid, 0.0) < CONFIG["event_cooldown_sec"]:
                try:
                    r.publish("faces:new", json.dumps({
                        "type": "box",
                        "camera_id": cam_id,
                        "person_id": pid,
                        "name": name,
                        "is_known": is_known,
                        "bbox": bbox_json,
                        "frame_w": fw,
                        "frame_h": fh,
                    }))
                except Exception:
                    pass
                continue
            last_event_at[pid] = now
            if len(last_event_at) > 500:
                cutoff_t = now - 300
                for k in [k for k, v in last_event_at.items() if v < cutoff_t]:
                    del last_event_at[k]

            pending.append(_PendingEvent(pid, name, is_known, bbox, bbox_json, emb,
                                         is_known and wants_alert, tags))

    # Подавляющее большинство кадров не создаёт ни одного события (все лица
    # отсекает cooldown) — для них ни снимок, ни вторая сессия не нужны.
    if not pending:
        return

    # --- Фаза 2: сеть и инференс. Открытой сессии БД здесь нет. --------
    # Снимок в полном разрешении берётся ОДИН раз на кадр и разбирается на
    # все лица сразу. Раньше и HTTP-запрос к камере, и прогон детектора по
    # полноразмерному кадру шли внутри цикла по лицам: кадр с пятью людьми
    # давал пять скачиваний одного и того же снимка и пять прогонов тяжёлой
    # модели по нему же — на 16 камерах это ровно та нагрузка, которую
    # раздел 18 ТЗ велит избегать, и ради неё же снимок и брался вместо
    # постоянного декодирования основного потока.
    #
    # Снимок лица берётся из полноразмерного кадра камеры, а не из кадра
    # аналитики: детекция идёт на субпотоке 640×360, и лицо в нём занимает
    # несколько десятков пикселей — такой кроп не спасает никакой апскейл,
    # информации в исходнике нет.
    hires, hi_faces = None, []
    if snapshot_url:
        hi_frame = fetch_snapshot_frame(snapshot_url)
        if hi_frame is not None:
            hires = hi_frame
            detector = FACE_APP
            try:
                hi_faces = detector.get(hi_frame) if detector else []
            except Exception:
                logger.warning("детекция на снимке камеры не удалась",
                               exc_info=True, extra={"camera_id": cam_id})
                hi_faces = []

    # Лица снимка, уже отданные персонам этого кадра: один кроп не должен
    # достаться двоим (см. pick_matching_face).
    claimed: set[int] = set()
    for p in pending:
        if hires is not None:
            sh, sw = hires.shape[:2]
            expected = onvif_client.scale_bbox(p.bbox, (fw, fh), (sw, sh))
            idx = pick_matching_face(
                [hf.bbox.tolist() for hf in hi_faces], expected, taken=claimed,
            )
            if idx is not None:
                claimed.add(idx)
                best = hi_faces[idx]
                p.snap_rel = save_face_snapshot(hires, cam_id, best.bbox.tolist())
                # Эмбеддинг с полноразмерного кадра точнее — он идёт в
                # событие и, значит, в поиск по фото.
                hi_emb = np.asarray(best.normed_embedding, dtype=np.float32)
                if hi_emb.shape[0] == 512:
                    p.emb = hi_emb
        # Если на снимке лица не нашлось — человек успел уйти за те
        # 100-300 мс, что снимок ехал. Режем из кадра аналитики: мыльный,
        # но заведомо тот кадр, где лицо действительно было. Кроп по
        # пересчитанной рамке был бы чётче и при этом мог бы содержать
        # что угодно.
        if p.snap_rel is None:
            p.snap_rel = save_face_snapshot(frame, cam_id, p.bbox)

    # --- Фаза 3: БД. Вставка событий. ----------------------------------
    # Одной транзакцией на кадр, а не по одной на лицо: событий здесь
    # немного (cooldown уже отсеял повторы), а публикация в Redis вынесена
    # за пределы сессии — иначе чтение ev.id после commit() снова открыло
    # бы транзакцию, теперь уже на время сетевого вызова к Redis.
    payloads = []
    with Session() as s:
        for p in pending:
            ev = FaceEvent(
                camera_id=cam_id,
                person_id=p.pid,
                ts=datetime.utcnow(),
                snapshot_path=p.snap_rel,
                orig_snapshot_path=p.snap_rel,
                enhanced=False,
                embedding=p.emb.tolist(),
                bbox=p.bbox_json,
                is_known=p.is_known,
            )
            s.add(ev)
            person = s.get(Person, p.pid)
            # Аватар — копия снимка в `avatars/`, а не ссылка на него:
            # `snapshots/` чистится по возрасту, и карточка теряла фото
            # через retention_days навсегда. Подробности и почему копия, а
            # не перенос — в шапке `avatar_store.py`.
            is_new_avatar = (
                person is not None and bool(p.snap_rel)
                and needs_avatar(person.avatar_path, MEDIA_PATH)
            )
            if is_new_avatar:
                adopted = adopt_snapshot(MEDIA_PATH, p.snap_rel)
                # None — копию сделать не удалось; аватар не назначается
                # вовсе: битая ссылка хуже пустой карточки.
                is_new_avatar = adopted is not None
                if is_new_avatar:
                    person.avatar_path = adopted
            # flush, а не commit: id события нужен для payload'а, а читать
            # его после commit() значит реактивировать объект и открыть
            # транзакцию заново.
            s.flush()
            payloads.append((
                {
                    "type": "face",
                    "event_id": ev.id,
                    "camera_id": cam_id,
                    "person_id": p.pid,
                    "name": p.name,
                    "is_known": p.is_known,
                    "alert": p.alert,
                    "tags": p.tags,
                    "snapshot": p.snap_rel,
                    "ts": ev.ts.isoformat(),
                    "bbox": p.bbox_json,
                    "frame_w": fw,
                    "frame_h": fh,
                },
                ev.id,
                is_new_avatar,
            ))
        s.commit()

    # --- Публикация. Сессия уже закрыта. -------------------------------
    for payload, ev_id, is_new_avatar in payloads:
        try:
            r.publish("faces:new", json.dumps(payload))
            # Ленивый апскейл (ТЗ 18.6): в режиме "manual" не делаем ничего,
            # в "avatar" улучшаем только первый кадр персоны (её аватар),
            # в "all" — всю галерею. Очередь ограничена, чтобы медленный
            # CPU-апскейл не копил бэклог часами.
            mode = CONFIG["upscale_mode"]
            want = mode == "all" or (mode == "avatar" and is_new_avatar)
            if want and r.llen("upscale:queue") < 500:
                r.lpush("upscale:queue", json.dumps({"event_id": ev_id}))
        except Exception:
            pass


def _to_vec(val) -> np.ndarray:
    """pgvector через psycopg2 без register_vector возвращает строку '[a,b,...]';
    через ORM — список. Приводим оба варианта к np.ndarray."""
    if isinstance(val, str):
        return np.fromstring(val.strip("[]"), sep=",", dtype=np.float32)
    return np.asarray(val, dtype=np.float32)


def _lower_priority():
    """Фоновые задачи не должны конкурировать с детекцией за CPU (ТЗ 18.6).
    В Linux-контейнере это nice, на Windows-хосте — idle priority процесса."""
    try:
        os.nice(10)
    except (AttributeError, OSError):
        pass


def _recluster_bg():
    _lower_priority()
    try:
        recluster_unknowns()
    except Exception:
        logger.error("ошибка фоновой рекластеризации", exc_info=True)


def recluster_unknowns():
    """DBSCAN по эмбеддингам последних событий неизвестных персон.
    Сливает кластеры в одну персону, обновляет центроид, перенаправляет события."""
    since = datetime.utcnow() - timedelta(days=7)
    with Session() as s:
        rows = s.execute(text("""
            SELECT person_id, embedding FROM face_events
            WHERE ts >= :since AND embedding IS NOT NULL
              AND person_id IN (SELECT id FROM persons WHERE status='unknown')
        """), {"since": since}).fetchall()
        if len(rows) < DBSCAN_MIN_SAMPLES * 2:
            return
        pids = np.array([r[0] for r in rows])
        embs = np.array([_to_vec(r[1]) for r in rows], dtype=np.float32)
        if embs.ndim != 2 or embs.shape[1] != 512:
            logger.warning("неожиданная форма эмбеддингов", extra={"shape": str(embs.shape)})
            return
        labels = DBSCAN(eps=DBSCAN_EPS, min_samples=DBSCAN_MIN_SAMPLES, metric="cosine").fit_predict(embs)
        merged = 0
        for label in set(labels):
            if label < 0:
                continue
            cluster_pids = sorted(set(int(p) for p, l in zip(pids, labels) if l == label))
            if len(cluster_pids) < 2:
                continue
            target = cluster_pids[0]
            others = cluster_pids[1:]
            cluster_embs = embs[labels == label]
            centroid = cluster_embs.mean(axis=0)
            centroid /= (np.linalg.norm(centroid) + 1e-9)
            s.execute(update(FaceEvent).where(FaceEvent.person_id.in_(others)).values(person_id=target))
            s.execute(delete(Person).where(Person.id.in_(others)))
            s.execute(text("UPDATE persons SET centroid = CAST(:c AS vector) WHERE id = :id"),
                      {"c": str(centroid.tolist()), "id": target})
            merged += len(others)
        if merged:
            s.commit()
            logger.info("рекластеризация: объединено персон", extra={"merged_count": merged})


def _drop_segments(s, segments) -> int:
    """Удаляет файлы сегментов и их строки. Возвращает число удалённых.

    Файл удаляется до строки: обратный порядок оставлял бы при падении
    процесса файл без строки — он не виден архиву, не попадает ни под
    retention, ни под циклическую перезапись, и место держит навсегда.
    Строка без файла, наоборот, самоисправляется — следующий проход её
    снесёт, а до того архив отдаст на неё честный 404.
    """
    dropped = 0
    for seg in segments:
        try:
            if seg.file_path and os.path.exists(seg.file_path):
                os.remove(seg.file_path)
        except Exception:
            pass
        # Миниатюра (SPEC §7) живёт отдельным файлом в `thumbs/` и под
        # `prune_media`, который чистит `segments/` по возрасту файла, не
        # попадает. Без этой строки она пережила бы свой сегмент навсегда:
        # id сегмента больше никогда не повторится, значит никто её и не
        # перезапишет.
        drop_thumb(MEDIA_PATH, seg.id)
        s.delete(seg)
        dropped += 1
    return dropped


def cleanup_old():
    """Ротация архива по сроку хранения (SPEC §5: глобально и по камерам)."""
    now = datetime.utcnow()
    global_days = CONFIG["retention_days"]
    with Session() as s:
        # Собственные сроки камер. Читаются на каждом проходе, а не
        # кэшируются: срок правится в админке и должен применяться со
        # следующей уборки, без перезапуска воркера (SPEC §2).
        per_camera = dict(
            s.execute(select(Camera.id, Camera.retention_days)).all()
        )
        # Отбор с запасом: по самому длинному сроку из действующих. Камера
        # со сроком меньше глобального иначе не чистилась бы вовсе, а
        # выбирать все строки архива (сотни тысяч на 120 камерах) ради
        # фильтрации в Python нельзя.
        horizon = max([global_days] + [d for d in per_camera.values() if d])
        candidates = s.execute(
            select(VideoSegment).where(
                VideoSegment.started_at < now - timedelta(days=min(
                    [global_days] + [d for d in per_camera.values() if d]))
            ).order_by(VideoSegment.started_at)
        ).scalars().all()
        expired = expired_segments(candidates, now, global_days, per_camera)
        dropped = _drop_segments(s, expired)
        # События лиц идут по глобальному сроку: они привязаны к камере
        # analytics, но карточка персоны собирается из событий всех камер, и
        # разная глубина по камерам рвала бы её непредсказуемо.
        s.execute(delete(FaceEvent).where(FaceEvent.ts < now - timedelta(days=global_days)))
        # Окна наблюдения (SPEC §6) переживать архив не должны: они нужны
        # ровно до того момента, когда сегмент осуждён или сохранён.
        # Глубина — глобальная, как у событий лиц: строка окна весит
        # десятки байт, а привязка к покамерным срокам добавила бы второй
        # запрос ради экономии мегабайта.
        s.execute(delete(MotionWindow).where(
            MotionWindow.ended_at < now - timedelta(days=global_days)))
        s.commit()
    if dropped:
        logger.info("ротация архива по сроку хранения",
                    extra={"segments_removed": dropped, "horizon_days": horizon})

    # Возраст файлов считается в naive-UTC (fileage.mtime_utc), тем же видом
    # времени, что и cutoff от utcnow(). Раньше здесь стоял
    # datetime.fromtimestamp() без tz, то есть локальное время хоста, и
    # сравнение уезжало на смещение часового пояса — см. fileage.py.
    removed = prune_media(MEDIA_PATH, now - timedelta(days=global_days),
                          now - timedelta(hours=1))
    if any(removed.values()):
        logger.info("уборка медиа-файлов", extra={"removed": removed})


def prune_orphan_media() -> dict[str, int]:
    """Файлы, потерявшие свою строку в БД (SPEC §3 «удаление камер», §5).

    Четвёртый механизм удаления рядом с retention, циклической перезаписью
    и уборкой сегментов без движения — и единственный, который идёт от
    файла к строке, а не наоборот. Он закрывает ровно один случай: строку
    удалил не воркер, а `ON DELETE CASCADE` при удалении камеры через
    веб-интерфейс. Обоснование направления и правил безопасности — в
    шапке `orphan_media.py`.

    Убирается двоякое сиротство, потому что у камеры сносятся строки
    сразу двух видов:

    * **сегменты** удалённых камер — их идентификатор читается из имени
      файла (`cam3_1754460000.mp4`);
    * **миниатюры** (§7), которые именуются идентификатором сегмента и
      живут отдельным файлом: `_drop_segments` удаляет их по строке, а
      строк после CASCADE нет. Их набор на диске мал (миниатюра
      появляется только у сегмента, который открывали в выдаче архива),
      поэтому спрашивается наличие именно этих идентификаторов.

    Третий вид сиротства — **аватары** (§15): `DELETE /api/persons/{pid}`
    снимает карточку, а её файл в `avatars/` не убирает никто (уборка по
    возрасту этот каталог не трогает намеренно — аватар живёт столько же,
    сколько карточка, см. `avatar_store.py`). Спрашивается наличие ссылки,
    а не персоны: на файл ссылается `persons.avatar_path`, и это же
    правило само собой закрывает случай «карточке назначили другой
    аватар, прежний остался».

    Раз в час, вместе с retention: сиротство возникает только в момент
    удаления камеры или персоны, и час задержки ничего не решает, а обход
    каталога архива не бесплатный.
    """
    stats = {"segments": 0, "thumbs": 0, "avatars": 0, "failed": 0}
    by_cam = segment_camera_ids(segments_dir(MEDIA_PATH))
    thumbs = thumb_segment_ids(MEDIA_PATH)
    avatars = avatar_files(MEDIA_PATH)
    if not by_cam and not thumbs and not avatars:
        return stats

    with Session() as s:
        live_cams = set(
            s.execute(select(Camera.id).where(Camera.id.in_(by_cam))).scalars().all()
        ) if by_cam else set()
        live_segs = set(
            s.execute(
                select(VideoSegment.id).where(VideoSegment.id.in_(thumbs))
            ).scalars().all()
        ) if thumbs else set()
        # Ссылки, а не персоны: спрашиваются только те относительные пути,
        # что встречены на диске, — список конечный.
        used_avatars = set(
            s.execute(
                select(Person.avatar_path).where(Person.avatar_path.in_(avatars))
            ).scalars().all()
        ) if avatars else set()

    doomed_cams = sorted(set(by_cam) - live_cams)
    doomed_files = [p for cam in doomed_cams for p in by_cam[cam]]
    removed, failed = remove_files(doomed_files)
    stats["segments"] = removed
    stats["failed"] += failed

    doomed_thumbs = [p for sid, p in thumbs.items() if sid not in live_segs]
    removed, failed = remove_files(doomed_thumbs)
    stats["thumbs"] = removed
    stats["failed"] += failed

    doomed_avatars = [p for rel, p in avatars.items() if rel not in used_avatars]
    removed, failed = remove_files(doomed_avatars)
    stats["avatars"] = removed
    stats["failed"] += failed

    if any(stats.values()):
        # warning, а не info: удаление камеры — редкая операция, и объём
        # освобождённого здесь места (сутки записи одной камеры — ~21.6 ГБ
        # по §16) администратор должен видеть в журнале без grep'а.
        logger.warning("уборка осиротевших медиа-файлов",
                       extra={"camera_ids": doomed_cams, **stats})
    return stats


def prune_motionless_segments() -> int:
    """«Запись только при движении»: сносит сегменты без движения (SPEC §6).

    Третий механизм удаления рядом с retention и циклической перезаписью
    (см. storage.py), и единственный, который смотрит на содержимое
    записи, а не на её возраст и не на место на диске. Включается
    покамерно флагом `record_on_motion` и работает только по камерам
    аналитики: у остальных нет источника движения (см. проверку в
    routers/cameras.py бэкенда).

    Порядок такой же осторожный, как в остальной уборке: сначала
    отбираются сегменты **старше** `MOTION_SETTLE_SEC` (у свежих ещё не
    все окна наблюдения записаны, и «покрытия нет» означало бы «не успели
    записать», а не «не смотрели»), затем к ним подтягиваются окна за тот
    же промежуток, и только полностью покрытые наблюдением и не задетые
    движением уходят в удаление.
    """
    with Session() as s:
        cam_ids = [
            cid for (cid,) in s.execute(
                select(Camera.id).where(
                    Camera.record_on_motion.is_(True),
                    Camera.mode == "analytics",
                )
            ).all()
        ]
        if not cam_ids:
            return 0

        cutoff = datetime.utcnow() - timedelta(seconds=MOTION_SETTLE_SEC)
        # LIMIT по той же причине, что и в циклической перезаписи: на
        # камере, у которой режим включили после недели непрерывной
        # записи, кандидатов сразу тысячи. Не хватит одного прохода —
        # добьёт следующий.
        segments = s.execute(
            select(VideoSegment)
            .where(VideoSegment.camera_id.in_(cam_ids),
                   VideoSegment.ended_at < cutoff)
            .order_by(VideoSegment.started_at)
            .limit(1000)
        ).scalars().all()
        if not segments:
            return 0

        # Окна тянутся одним запросом на весь пакет и только за нужный
        # отрезок: за 14 дней на камеру их 20 000, и выбирать всё подряд
        # ради сотни сегментов незачем. Границы расширены на запас
        # (guard + слияние соседних окон), иначе окно, начавшееся до
        # первого сегмента, не попало бы в выборку и разорвало покрытие.
        margin = timedelta(seconds=DEFAULT_GUARD_SEC + 120)
        first = min(seg.started_at for seg in segments) - margin
        last = max(seg.ended_at for seg in segments) + margin
        windows = s.execute(
            select(MotionWindow).where(
                MotionWindow.camera_id.in_(cam_ids),
                MotionWindow.ended_at >= first,
                MotionWindow.started_at <= last,
            )
        ).scalars().all()

        victims = segments_without_motion(segments, windows,
                                          guard_sec=DEFAULT_GUARD_SEC)
        dropped = _drop_segments(s, victims)
        s.commit()

    if dropped:
        logger.info("удалены сегменты без движения (запись по движению)",
                    extra={"segments_removed": dropped,
                           "cameras": len(cam_ids),
                           "considered": len(segments)})
    return dropped


def enforce_disk_quota() -> int:
    """Циклическая перезапись: сносит старейшие сегменты при переполнении.

    SPEC §5 («циклическая перезапись») и §21 («автоудаление старейших
    сегментов при переполнении»). Механизм аварийный и намеренно
    независимый от retention: он срабатывает ровно тогда, когда расчёт
    хранения не сошёлся с фактическим битрейтом — VBR и smart-кодек дают
    разброс, из-за которого 14 дней по номиналу могут не влезть. Без него
    запись встала бы целиком, что хуже, чем потеря самых старых суток.

    Идёт по всему архиву по времени, а не по камерам: решение «чьей
    записью жертвовать» администратор уже выразил через retention, и
    принимать его второй раз здесь было бы дублированием с другим ответом.
    """
    try:
        du = shutil.disk_usage(MEDIA_PATH)
    except OSError:
        logger.error("не удалось прочитать заполнение диска архива", exc_info=True)
        return 0

    target_free_pct = float(CONFIG["disk_min_free_pct"])
    need = bytes_to_free(du.total, du.free, target_free_pct)
    if not need:
        return 0

    with Session() as s:
        # LIMIT, а не весь архив: на переполненном диске кандидатов —
        # сотни тысяч строк, а освободить нужно проценты от объёма.
        # Не хватит одного прохода — добьёт следующий через час.
        oldest = s.execute(
            select(VideoSegment).order_by(VideoSegment.started_at).limit(5000)
        ).scalars().all()
        victims = oldest_segments_to_free(oldest, need)
        dropped = _drop_segments(s, victims)
        s.commit()

    if dropped:
        logger.warning(
            "циклическая перезапись: диск заполнен, удалены старейшие сегменты",
            extra={"segments_removed": dropped, "need_bytes": need,
                   "free_pct": round(du.free * 100.0 / max(1, du.total), 1)},
        )
    return dropped


def check_disk_alerts() -> str | None:
    """Алерт по заполнению диска архива (SPEC §14: «диск > 80%/90%»).

    Возвращает уровень, чтобы вызывающий (и тест) видел решение, а не
    только побочный эффект в логе.
    """
    try:
        du = shutil.disk_usage(MEDIA_PATH)
    except OSError:
        return None
    if du.total <= 0:
        return None
    used_pct = du.used * 100.0 / du.total
    level = disk_alert_level(used_pct, float(CONFIG["disk_warn_pct"]),
                             float(CONFIG["disk_crit_pct"]))
    if not level:
        return None
    # Кулдаун: диск заполняется медленно, и без него сообщение уходило бы
    # каждый проход менеджера. Отдельный ключ на уровень — переход
    # warning → critical не должен ждать конца кулдауна предупреждения.
    try:
        if r.set(f"alert:disk:{level}", "1",
                 ex=CONFIG["alert_cooldown_sec"], nx=True) is None:
            return level
    except Exception:
        pass
    log = logger.error if level == "critical" else logger.warning
    log("заполнение диска архива",
        extra={"level": level, "used_pct": round(used_pct, 1),
               "free_gb": round(du.free / BYTES_PER_GB, 1)})
    # SPEC §9 требует алерт по переполнению диска, а не только запись в лог:
    # журнал на объекте никто не читает, пока архив не начал стираться.
    # Из фоновой нити — check_disk_alerts() зовётся из цикла менеджера, и
    # 15-секундный таймаут SMTP задержал бы обход камер.
    threading.Thread(
        target=send_email_alert,
        args=(f"FaceWatch: диск архива заполнен на {used_pct:.0f}%",
              f"Уровень: {level}. Занято {used_pct:.1f}%, "
              f"свободно {du.free / BYTES_PER_GB:.1f} ГБ.\n"
              f"При достижении порога перезаписи старые сегменты будут удалены."),
        daemon=True,
    ).start()
    return level


def _record_alert_due(kind: str, camera_id: int) -> bool:
    """True, если по этой камере и этому виду события кулдаун истёк.

    Кулдаун **на камеру**, а не общий на вид события: общий проглотил бы
    вторую камеру, отвалившуюся через минуту после первой, — а это как раз
    развитие аварии, ради которого алерт и заведён.

    Для «пропуска записи» кулдаун обязателен по другой причине:
    `segment_gaps()` возвращает текущие пропуски **на каждом проходе**, а не
    переходы, поэтому без него одна невосстановленная камера слала бы
    сообщение каждые десять секунд, пока её не починят.

    Недоступность Redis трактуется как «кулдаун истёк» — так же, как в
    `_alert_cooldown_passed`: молчащий алертинг хуже повторов.
    """
    try:
        return r.set(f"alert:record:{kind}:{camera_id}", "1",
                     ex=CONFIG["alert_cooldown_sec"], nx=True) is not None
    except Exception:
        return True


def send_record_layer_alert(kind: str, camera_ids: list[int]) -> list[int]:
    """Оповещение по слою записи (SPEC §9). Возвращает камеры, о которых
    сообщили, — вызывающему и тесту нужно видеть решение, а не гадать.

    §9 требует алертов на «потерю потока» и «пропуск записи» наравне с
    переполнением диска. У диска оповещение есть с цикла 29 и там же
    записана причина: «журнал на объекте никто не читает, пока архив не
    начал стираться». К этим двум тот же довод приложим сильнее — камера,
    переставшая писаться ночью, не оставляет по себе ничего, кроме дыры в
    архиве, которую найдут в день, когда запись понадобится.

    Что и как сказать — решает `record_status.alert_batch()`: она чистая и
    потому проверяется в лёгкой CI-джобе воркера, которая не ставит cv2.
    Здесь остаётся сетевое — кулдаун через Redis и сама отправка.
    """
    batch = alert_batch(kind, camera_ids, lambda cid: _record_alert_due(kind, cid))
    if batch is None:
        return []
    fresh, subject, text_msg = batch
    # Из фоновой нити по той же причине, что и алерт диска: обе отправки
    # сетевые, а зовут их из цикла менеджера, который обходит камеры.
    threading.Thread(
        target=_send_record_alert_channels, args=(subject, text_msg), daemon=True,
    ).start()
    return fresh


def _send_record_alert_channels(subject: str, text_msg: str) -> None:
    send_telegram_alert(text_msg)
    send_email_alert(f"FaceWatch: {subject.lower()}", text_msg)


def record_layer_sync(cams) -> None:
    """Приводит пути MediaMTX к списку включённых камер (SPEC §2, §20).

    Идемпотентна и дёшева на неизменившемся списке: sync_paths() сравнивает
    только те поля, которыми управляет слой записи, и на совпадении не
    делает ни одного пишущего запроса. Поэтому вызов стоит прямо в цикле
    менеджера — добавление или отключение камеры в админке подхватывается
    за ~10 с без перезапуска слоёв, чего и требует SPEC §2.

    Пишется ВСЕГДА основной поток (SPEC §20: «Запись ТОЛЬКО основного
    потока. Субпоток в архив не пишется») — независимо от того, какой поток
    использует аналитика.
    """
    global _record_desired

    desired = {}
    for cam_id, rtsp_url in cams:
        desired[path_name(cam_id)] = path_conf(
            rtsp_url, segment_duration_min=CONFIG["record_segment_min"],
            media_root=RECORD_MEDIA_ROOT,
        )
    # Желаемая конфигурация публикуется ДО обращения к сети: супервизор
    # восстановления (§19) пересоздаёт путь ровно этой конфигурацией, и
    # если бы она обновлялась только после успешного sync_paths(), то при
    # недоступном на этом проходе Control API супервизор продолжал бы
    # заводить путь по устаревшему RTSP-URL — то есть чинил бы камеру
    # адресом, который администратор уже сменил.
    _record_desired = desired
    stats = sync_paths(MediaMTXClient(MEDIAMTX_API_URL), desired)
    if any(stats.values()):
        logger.info("синхронизация слоя записи", extra=stats)


def publish_record_layer_status(cam_names) -> dict:
    """Статус каждого потока слоя записи → Redis (SPEC §14, §9).

    SPEC §14 требует «статус каждого RTSP-потока слоя записи (120 шт.)» на
    дашборде администратора, §9 — сводку «активные потоки (из 120)».
    Источник — Control API MediaMTX; бэкенд к нему не ходит сам намеренно:
    Control API даёт доступ к RTSP-адресам всех камер с учётными данными и
    наружу не публикуется (решение цикла 24), поэтому наружу состояние
    выносит воркер через Redis — тем же способом, что и `worker:fps`.

    Возвращает состояния, чтобы вызывающий (и тест) видел решение, а не
    только запись в Redis.
    """
    global _record_prev_status

    global _record_byte_samples

    global _record_api_error

    try:
        runtime = MediaMTXClient(MEDIAMTX_API_URL).runtime_paths()
        _record_api_error = None
    except Exception as exc:
        # Control API недоступен — это `unknown`, а не «120 камер offline»:
        # см. пояснение в record_status.py.
        #
        # Причина ОБЯЗАНА быть видимой. Пока она писалась на debug, отказ
        # выглядел так: статусы камер молча замирали, интерфейс показывал
        # всю стену офлайн, и в логах не было ни строчки — понять, что
        # сломался именно Control API, было нечем. Уровень warning с
        # подавлением повторов: сообщение повторяется раз в ~10 секунд,
        # и без подавления оно заполнило бы журнал целиком.
        reason = f"{type(exc).__name__}: {exc}"[:300]
        if reason != _record_api_error:
            logger.warning(
                "Control API MediaMTX недоступен — статусы потоков записи "
                "неизвестны, синхронизация путей не идёт",
                extra={"url": redact_url(MEDIAMTX_API_URL), "reason": reason},
            )
        _record_api_error = reason
        runtime = None

    states = stream_states(cam_names, runtime)

    # Потеря и восстановление потока — в аудит и алертинг (SPEC §14).
    # Считается переход, а не текущее состояние: иначе физически
    # выключенная камера слала бы алерт каждые десять секунд.
    lost = newly_lost(_record_prev_status, states)
    for cam_id in lost:
        logger.error("потерян поток слоя записи",
                     extra={"camera_id": cam_id, "event": "record_stream_lost"})
    if lost:
        send_record_layer_alert("stream_lost", lost)
    for cam_id in newly_restored(_record_prev_status, states):
        logger.info("поток слоя записи восстановлен",
                    extra={"camera_id": cam_id, "event": "record_stream_restored"})

    # Пропуск записи сегмента (SPEC §14) — отдельный алерт: поток может быть
    # online, а файлы не расти (нет места, права, сбой записи в MediaMTX).
    try:
        # Время везде в naive-UTC — том же виде, в котором лежат метки в БД
        # (соглашение `fileage.py`). `ended_at.timestamp()` здесь был бы
        # ошибкой: на naive-datetime он трактует значение как локальное
        # время хоста, и на непустой `TZ` проверка «сегмент не пишется
        # дольше N минут» уехала бы ровно на смещение пояса — в одну
        # сторону молчала бы всегда, в другую алертила бы всегда.
        newest_segments = _last_segments(states.keys())
        gaps = segment_gaps({cid: ts for cid, (ts, _) in newest_segments.items()},
                            states, _utc_seconds(datetime.utcnow()),
                            CONFIG["record_segment_min"])
        for cam_id in gaps:
            logger.error("пропуск записи сегмента",
                         extra={"camera_id": cam_id, "event": "record_segment_missing"})
        if gaps:
            send_record_layer_alert("segment_missing", list(gaps))
    except Exception:
        logger.error("не удалось проверить пропуски сегментов", exc_info=True)
        gaps = []
        newest_segments = {}

    _record_prev_status = {cid: st["status"] for cid, st in states.items()}

    # FPS и битрейт каждого потока (SPEC §9). Обе величины Control API не
    # отдаёт вовсе, поэтому считаются здесь: битрейт — из разности
    # счётчика байтов между проходами, FPS — пробой последнего дописанного
    # сегмента. Подробности и обоснование источников — в stream_rate.py.
    #
    # Под общим try: ни одна из двух величин не стоит того, чтобы уронить
    # проход менеджера, который в этом же цикле синхронизирует пути записи
    # и ставит статусы камер (SPEC §2 — слои независимы, и уж тем более от
    # украшения строки мониторинга запись зависеть не должна).
    try:
        rates, _record_byte_samples = update_bitrates(
            _record_byte_samples, states, time.time())
        fps_by_camera = _record_fps_cache.refresh(
            newest_segments, _utc_seconds(datetime.utcnow()),
            CONFIG["record_segment_min"] * 60 * SEGMENT_GAP_FACTOR)
    except Exception:
        logger.error("не удалось посчитать скорость потоков записи", exc_info=True)
        rates, fps_by_camera = {}, {}
    for cam_id, st in states.items():
        st["bitrate_kbps"] = rates.get(cam_id)
        st["fps"] = fps_by_camera.get(cam_id)

    # Статус камеры в БД — из слоя записи (SPEC §2, §4).
    #
    # До этого его выставляла только нить аналитики, а цикл 24 перестал
    # поднимать её для камер в режиме `record_only`. В результате камера,
    # которая исправно пишется, навсегда оставалась `offline`, и
    # live-просмотр (SPEC §4) не работал ни на одной из них — то есть на
    # 118 камерах из 120 при штатной конфигурации.
    #
    # `unknown` не пишется: недоступный Control API означает «не знаем», а
    # не «камера пропала», и затирать им последний известный статус
    # значило бы гасить всю стену камер на каждый рестарт MediaMTX.
    for cam_id, st in states.items():
        if st["status"] == UNKNOWN:
            _record_layer_owned.discard(cam_id)
            continue
        _record_layer_owned.add(cam_id)
        update_status(cam_id, st["status"], source="record_layer")
    # Камеры, выбывшие из слоя записи (выключены, удалены), владения за
    # собой не оставляют — иначе их статус замёрз бы навсегда.
    _record_layer_owned.intersection_update(states)

    payload = {"streams": list(states.values()), "summary": summarize(states),
               "segment_gaps": gaps, "updated_at": time.time(),
               # Состояние слоя аналитики едет здесь же: у него уже есть
               # читатель и TTL. Отдельный ключ ради двух полей означал бы
               # второй запрос из бэкенда на каждый показ страницы.
               # Причина, по которой статусы потоков неизвестны. Без неё
               # интерфейс показывал бы «неизвестно» на всех камерах без
               # единого намёка, куда смотреть.
               "control_api_error": _record_api_error,
               # Расхождение корней медиаданных (см. log_record_root): при
               # нём архив может молча остаться пустым, и одних алертов
               # «пропуск записи» мало — они не говорят, куда смотреть.
               "record_root_warning": _record_root_warning,
               # Восстановление потоков (§19): по камерам в обрыве —
               # сколько он длится и отвечает ли камера на RTSP. Без этого
               # «камера выключена» и «камера отвечает, а запись не идёт»
               # на стене выглядят одинаково, а чинятся по-разному.
               "recovery": (_record_recovery.snapshot()
                            if _record_recovery is not None else {}),
               "analytics": {"model_ready": FACE_APP is not None,
                             "model": CONFIG["face_model"],
                             "error": MODEL_ERROR}}
    try:
        r.set("record:layer", json.dumps(payload), ex=120)
    except Exception:
        # Redis лежит — состояние просто не доедет до интерфейса; ронять из-за
        # этого проход менеджера (а с ним синхронизацию записи) нельзя.
        logger.debug("не удалось опубликовать статус слоя записи", exc_info=True)
    return payload


def _utc_seconds(dt: datetime) -> float:
    """naive-UTC → секунды, сравнимые между собой.

    Не `dt.timestamp()`: тот интерпретирует naive-значение как локальное
    время хоста (см. `fileage.py`). Здесь обе стороны сравнения проходят
    через одно и то же преобразование, поэтому разность корректна при
    любом часовом поясе контейнера.
    """
    return dt.replace(tzinfo=timezone.utc).timestamp()


def _last_segments(camera_ids) -> dict[int, tuple[float, str]]:
    """Последний дописанный сегмент по камере: «время конца, путь файла».

    Читается из архива, а не с диска: строка появляется там только после
    того, как файл дописан и проиндексирован, — это и есть признак «запись
    идёт», который проверяет SPEC §14. Он же гарантирует, что путь ведёт на
    **закрытый** файл: пробовать ffprobe растущий сегмент (SPEC §9, FPS)
    значило бы мерить длительность, которая ещё меняется.

    Путь и время берутся одной строкой, а не двумя запросами: `max(ended_at)`
    в группировке не сказал бы, какому файлу принадлежит максимум.
    """
    ids = list(camera_ids)
    if not ids:
        return {}
    newest = (
        select(VideoSegment.camera_id,
               func.max(VideoSegment.ended_at).label("ended_at"))
        .where(VideoSegment.camera_id.in_(ids))
        .group_by(VideoSegment.camera_id)
        .subquery()
    )
    with Session() as s:
        rows = s.execute(
            select(VideoSegment.camera_id, VideoSegment.ended_at,
                   VideoSegment.file_path)
            .join(newest,
                  (VideoSegment.camera_id == newest.c.camera_id)
                  & (VideoSegment.ended_at == newest.c.ended_at))
        ).all()
    out: dict[int, tuple[float, str]] = {}
    for cam_id, ended, path in rows:
        if ended is None:
            continue
        # Переворот файла на границе секунды может дать две строки с
        # одинаковым `ended_at`; какая из них попадёт в карту — неважно,
        # обе описывают один и тот же поток в одно и то же время.
        out[cam_id] = (_utc_seconds(ended), path)
    return out


def index_record_segments() -> None:
    """Заносит дописанные сегменты слоя записи в архив (`video_segments`).

    Идёт в цикле менеджера, а не в нити камеры: файлы пишет чужой процесс,
    и привязывать индексацию к живости аналитики значило бы снова связать
    слои вопреки SPEC §2.
    """
    try:
        index_new_segments(
            Session, VideoSegment, segments_dir(MEDIA_PATH),
            now=time.time(), camera_model=Camera,
            from_timestamp=datetime.utcfromtimestamp,
        )
    except Exception:
        logger.error("не удалось занести сегменты записи в архив", exc_info=True)


def log_record_root() -> str | None:
    """Пишет в журнал, куда слой записи кладёт сегменты и где их ищет архив.

    Смысл строки — в том, что до цикла 39 `recordPath` был захардкожен
    `/media/segments/...` независимо от `MEDIA_PATH`. Стоило вынести архив
    на отдельный диск (SPEC §5 «путь архива конфигурируется», §26 раскладка
    `/var/lib/facewatch/`) — и MediaMTX продолжал писать в старый каталог, а
    воркер сканировал новый. Симптом был отложенный и ни на что не
    указывающий: через два интервала сегмента загорался алерт «пропуск
    записи» СРАЗУ НА ВСЕХ камерах, retention при этом не удалял ничего
    (файлов по своему пути он не видел), и диск заполнялся до отказа.

    Возвращает текст расхождения либо None — он же уезжает в состояние слоя
    записи, чтобы страница мониторинга могла его показать.
    """
    scan = segments_dir(MEDIA_PATH)
    write = record_path_template(RECORD_MEDIA_ROOT)
    logger.info("слой записи: каталоги сегментов",
                extra={"mediamtx_record_path": write, "indexer_scan_dir": scan,
                       "media_path": MEDIA_PATH,
                       "mediamtx_media_root": RECORD_MEDIA_ROOT})
    msg = record_root_divergence(MEDIA_PATH, RECORD_MEDIA_ROOT)
    if msg:
        logger.warning("слой записи: корни медиаданных разведены",
                       extra={"mediamtx_record_path": write,
                              "indexer_scan_dir": scan})
    return msg


# Последняя напечатанная сводка раскладки по NUMA — чтобы не писать её в
# лог каждые 10 секунд на каждом проходе менеджера.
_numa_logged: dict | None = None


def _log_numa_layout(analytics_ids) -> dict:
    """Напечатать раскладку каналов по NUMA-нодам при её изменении (§17).

    Печатается один раз на состояние, а не на проход: менеджер крутится
    каждые 10 с, и безусловная запись утопила бы журнал. Но и молчать
    нельзя — без строки в логе на объекте нельзя отличить «привязка
    работает» от «привязка молча не встала», а именно это отличие решает,
    искать ли причину низкого FPS в NUMA (см. DEPLOY_CHECKLIST.md).
    """
    global _numa_logged
    info = cpu_affinity.describe(analytics_ids)
    if info != _numa_logged:
        _numa_logged = info
        logger.info("раскладка аналитики по NUMA", extra=info)
    return info


def manager():
    # Отметки живости заводятся ПЕРВЫМИ и передаются в embed-API ниже:
    # именно `/health` этого API опрашивает healthcheck контейнера, и до
    # цикла 38 он отвечал «жив» независимо от того, крутится ли этот цикл
    # (uvicorn работает в своей нити). Зависший менеджер выглядел здоровым.
    global HEARTBEAT, _record_root_warning, _record_recovery
    HEARTBEAT = Heartbeat()
    HEARTBEAT.beat("startup")

    _record_root_warning = log_record_root()

    # Настройки читаем ДО загрузки модели: профиль задаёт face_model и
    # detect_width, иначе выбор в админке не применялся бы до перезапуска.
    refresh_config()

    # HTTP-API поднимается ПЕРВЫМ — до модели.
    #
    # Он обслуживает автообнаружение камер по ONVIF, которое к распознаванию
    # лиц отношения не имеет вовсе (SPEC §3). Пока запуск стоял после
    # загрузки модели, отказ загрузки уносил и его: администратор не мог
    # даже найти камеры в сети, а бэкенд отвечал «Сервис распознавания
    # недоступен» на запрос, распознавания не касающийся.
    #
    # Модель передаётся функцией, а не значением: она может появиться
    # позже (см. цикл дозагрузки ниже), и API обязан это подхватить.
    try:
        from embed_api import start_embed_api
        start_embed_api(lambda: FACE_APP, port=9000, heartbeat=HEARTBEAT)
        logger.info("embed-API запущен", extra={"port": 9000})
    except Exception:
        logger.error("не удалось запустить embed-API", exc_info=True)

    # Сторож — после embed-API и ДО загрузки модели: скачивание модели при
    # первом запуске идёт минуты, и это как раз тот этап, зависание на
    # котором раньше было неотличимо от работы.
    watchdog = start_watchdog(HEARTBEAT)

    # Отказ загрузки модели НЕ должен ронять процесс (SPEC §2: «Отказ
    # аналитики НЕ влияет на запись»).
    #
    # До этого фикса `load_face_app()` стоял здесь голым вызовом, и любая
    # его ошибка убивала весь воркер — вместе со слоем записи, индексацией
    # сегментов, статусами камер и ONVIF-API. На практике это происходило
    # штатно: InsightFace скачивает модель из интернета при первом запуске,
    # а production-сервер видеонаблюдения обычно изолирован. Контейнер
    # уходил в бесконечный CrashLoop, и запись не велась вообще.
    loaded_model = None
    HEARTBEAT.beat("model_load")
    _try_load_model()
    if FACE_APP is not None:
        loaded_model = (CONFIG["face_model"], CONFIG["detect_width"], analytics_threads())

    # Супервизор восстановления потоков (SPEC §19: «восстановление потока
    # ≤ 5 секунд после обрыва»). Поднимается ДО первого прохода менеджера,
    # но до первой синхронизации путей ему нечего делать: список желаемых
    # путей пуст, и tick() выходит сразу.
    #
    # Он в стороне от слоя аналитики намеренно (SPEC §2): загрузка модели
    # выше могла провалиться, и запись это затрагивать не должно — в том
    # числе её восстановление после обрыва.
    if RECORD_RECOVERY_INTERVAL > 0:
        _record_recovery = RecoverySupervisor(
            lambda: MediaMTXClient(MEDIAMTX_API_URL),
            lambda: _record_desired,
            interval=RECORD_RECOVERY_INTERVAL,
        ).start()
        logger.info("супервизор восстановления потоков запущен",
                    extra={"interval_sec": RECORD_RECOVERY_INTERVAL})
    else:
        logger.info("супервизор восстановления потоков выключен "
                    "(RECORD_RECOVERY_INTERVAL=0)")

    threads: dict[int, threading.Thread] = {}
    last_cleanup = 0.0
    last_recluster = 0.0
    last_motion_prune = 0.0
    last_model_retry = time.time()

    logger.info("конфиг воркера", extra={"config": CONFIG})

    while not shutdown_event.is_set():
        try:
            HEARTBEAT.beat("camera_scan")
            refresh_config()
            # Бюджет потоков ORT — часть параметров модели: он применяется
            # к сессиям при создании, и смена настройки без перезагрузки
            # модели ничего бы не изменила (SPEC §16).
            want_model = (CONFIG["face_model"], CONFIG["detect_width"],
                          analytics_threads())
            # Смена модели/разрешения в профиле применяется без перезапуска
            if FACE_APP is not None and want_model != loaded_model:
                logger.info("параметры модели изменились, перезагружаю")
                HEARTBEAT.beat("model_load")
                if _try_load_model():
                    loaded_model = want_model
            elif FACE_APP is None and time.time() - last_model_retry > MODEL_RETRY_SEC:
                HEARTBEAT.beat("model_load")
                # Повторная попытка после отказа: модель могла появиться без
                # перезапуска контейнера (администратор положил файлы в том,
                # починился доступ в интернет). Раз в 5 минут, а не каждый
                # проход: скачивание модели идёт минуты и блокирует цикл.
                last_model_retry = time.time()
                if _try_load_model():
                    loaded_model = want_model
                    logger.info("слой аналитики включён: модель загружена")
            record_cams: list[tuple[int, str]] = []
            record_cam_names: list[tuple[int, str]] = []
            # Камеры, которые должны быть под аналитикой на этом проходе, —
            # и те, чья нить уже крутится, и те, что ещё предстоит поднять.
            # Раскладка по NUMA-нодам (§17) обязана считаться от ПОЛНОГО
            # списка: если считать её только от поднимаемых, перезапуск
            # одной нити переносил бы камеру на другой сокет всякий раз,
            # когда соседи в этот момент живы.
            analytics_ids: list[int] = []
            pending: list[tuple[int, str, str | None, dict | None]] = []
            with Session() as s:
                cams = s.execute(select(Camera).where(Camera.enabled == True)).scalars().all()
                for cam in cams:
                    try:
                        rtsp = fernet.decrypt(cam.rtsp_url_enc.encode()).decode()
                        sub_enc = getattr(cam, "sub_rtsp_url_enc", None)
                        sub = fernet.decrypt(sub_enc.encode()).decode() if sub_enc else None
                    except Exception:
                        logger.error("не удалось расшифровать RTSP", exc_info=True, extra={"camera_id": cam.id})
                        continue
                    # Слой записи не зависит от того, поднялась ли нить
                    # аналитики: камера попадает в него сразу после расшифровки
                    # адреса (SPEC §2).
                    record_cams.append((cam.id, rtsp))
                    record_cam_names.append((cam.id, cam.name or f"Камера {cam.id}"))
                    # SPEC §6: детекция и распознавание — только на камерах в
                    # режиме analytics. На камерах record_only нить не
                    # поднимается вовсе: на целевых 120 камерах это ровно то,
                    # что §24 выносит за рамки версии («Детекция лиц на всех
                    # 120 камерах без GPU»).
                    if (getattr(cam, "mode", None) or "record_only") != "analytics":
                        continue
                    # Без модели нить аналитики не поднимается: она упала бы
                    # на первом же кадре. Слой записи выше этой строки уже
                    # отработал — камера пишется независимо (SPEC §2).
                    if FACE_APP is None:
                        continue
                    # В раскладку камера попадает независимо от того, жива
                    # ли её нить: см. комментарий к analytics_ids выше.
                    analytics_ids.append(cam.id)
                    if cam.id in threads and threads[cam.id].is_alive():
                        continue
                    onvif_config = None
                    if getattr(cam, "onvif_enabled", False) and getattr(cam, "onvif_host", None):
                        onvif_password = None
                        pw_enc = getattr(cam, "onvif_password_enc", None)
                        if pw_enc:
                            try:
                                onvif_password = fernet.decrypt(pw_enc.encode()).decode()
                            except Exception:
                                logger.error("не удалось расшифровать ONVIF-пароль", exc_info=True, extra={"camera_id": cam.id})
                        onvif_config = {
                            "host": cam.onvif_host,
                            "port": cam.onvif_port or 80,
                            "username": cam.onvif_username,
                            "password": onvif_password,
                        }
                    pending.append((cam.id, rtsp, sub, onvif_config))

            # Раскладка каналов по NUMA-нодам (SPEC §17) — после того, как
            # известен ВЕСЬ список камер analytics, и до старта нитей: нить
            # обязана получить свою ноду до первого кадра, иначе её буферы
            # успевают лечь на чужую память (политика first-touch,
            # см. cpu_affinity.py). На односокетной машине раскладка пуста и
            # ни одна нить не привязывается.
            layout = cpu_affinity.plan(analytics_ids)
            _log_numa_layout(analytics_ids)
            for cam_id, rtsp, sub, onvif_config in pending:
                t = threading.Thread(
                    target=camera_worker,
                    args=(cam_id, rtsp, FACE_APP, sub, onvif_config, layout.get(cam_id)),
                    daemon=True,
                )
                t.start()
                threads[cam_id] = t

            # Слой записи (SPEC §20) — вне сессии БД: sync_paths() ходит по
            # сети в MediaMTX, и держать на это время открытое соединение с
            # Postgres незачем (правило из цикла 20).
            HEARTBEAT.beat("record_layer_sync")
            try:
                record_layer_sync(record_cams)
            except Exception:
                logger.error("не удалось синхронизировать слой записи", exc_info=True)
            HEARTBEAT.beat("index_segments")
            index_record_segments()

            # Статус потоков записи — после индексации: проверка пропуска
            # сегмента смотрит на последнюю занесённую строку, и порядок
            # наоборот давал бы ложный пропуск ровно на один проход.
            HEARTBEAT.beat("record_status")
            try:
                publish_record_layer_status(record_cam_names)
            except Exception:
                logger.error("не удалось собрать статус слоя записи", exc_info=True)

            now = time.time()
            if now - last_cleanup > 3600:
                last_cleanup = now
                HEARTBEAT.beat("cleanup")
                try:
                    cleanup_old()
                except Exception:
                    logger.error("ошибка очистки (cleanup)", exc_info=True)
                # Отдельным try: сиротство после удаления камеры не связано
                # с retention ни причиной, ни данными, и отказ одной уборки
                # не должен отменять вторую — иначе диск продолжал бы расти
                # по причине, о которой в журнале уже написано.
                try:
                    prune_orphan_media()
                except Exception:
                    logger.error("ошибка уборки осиротевших медиа-файлов",
                                 exc_info=True)

            # SPEC §6: уборка сегментов без движения идёт чаще retention —
            # раз в 5 минут. Смысл режима в том, чтобы не занимать диск
            # пустой записью, и час задержки означал бы, что на камере с
            # редким движением архив всё равно растёт часовыми ступенями.
            # Проход дешёвый: на выключенном режиме это один запрос,
            # возвращающий ноль камер.
            if now - last_motion_prune > 300:
                last_motion_prune = now
                HEARTBEAT.beat("motion_prune")
                try:
                    prune_motionless_segments()
                except Exception:
                    logger.error("ошибка уборки сегментов без движения", exc_info=True)

            # Заполнение диска проверяется на каждом проходе (~10 с), а не
            # раз в час вместе с retention: между часовыми проходами 120
            # камер успевают дописать ~108 ГБ, и на переполненном томе
            # запись встала бы задолго до следующей уборки. На здоровом
            # диске оба вызова — это два statvfs и ни одного запроса к БД.
            #
            # Порог перезаписи (`disk_min_free_pct`) и пороги алертов
            # (`disk_warn_pct`/`disk_crit_pct`) независимы намеренно: они
            # настраиваются раздельно и могут стоять в любом порядке.
            # Связать их (например, «сносить только при critical») значило
            # бы, что понижение порога алерта молча отключает перезапись.
            HEARTBEAT.beat("disk_alerts")
            try:
                check_disk_alerts()
            except Exception:
                logger.error("ошибка проверки заполнения диска", exc_info=True)
            HEARTBEAT.beat("disk_quota")
            try:
                enforce_disk_quota()
            except Exception:
                logger.error("ошибка циклической перезаписи", exc_info=True)

            # Пакетная кластеризация (ТЗ 18.6): интервал задаётся профилем,
            # выполняется в фоновой нити с пониженным приоритетом.
            if now - last_recluster > CONFIG["cluster_interval_min"] * 60:
                last_recluster = now
                threading.Thread(target=_recluster_bg, daemon=True).start()

        except Exception:
            logger.error("ошибка цикла воркера", exc_info=True)
        # Прерываемое ожидание: shutdown не должен ждать до 10с впустую.
        HEARTBEAT.beat("idle")
        shutdown_event.wait(10)

    HEARTBEAT.beat("shutdown")
    # Супервизор останавливается ПЕРВЫМ и с ожиданием: между `delete` и
    # `add` путь камеры не существует, и выход процесса в этот момент
    # оставил бы камеру без записи до следующего старта воркера — то есть
    # ровно ту дыру в архиве, против которой модуль и написан.
    if _record_recovery is not None:
        _record_recovery.stop()
    logger.info("завершение: жду остановки нитей камер...")
    # Бюджет ожидания общий на все камеры (не по 8с на каждую), иначе
    # остановка 16 камер могла бы растянуться на пару минут и упереться
    # в SIGKILL раньше, чем нити успеют освободить ресурсы.
    deadline = time.time() + 8.0
    for cam_id, t in threads.items():
        t.join(timeout=max(0.0, deadline - time.time()))
        if t.is_alive():
            logger.warning("нить камеры не успела остановиться в срок", extra={"camera_id": cam_id})
    # Последняя индексация перед выходом: запись продолжает идти в MediaMTX
    # и после остановки воркера, но сегменты, дописанные к этому моменту,
    # лучше занести сейчас — иначе они ждут следующего старта воркера.
    # Ожидания нитей финализации здесь больше нет: воркер ничего не пишет и
    # ничего не перекодирует, терять на остановке нечего (до цикла 24 здесь
    # терялся последний сегмент каждой камеры, см. REVIEW_LOG.md цикл 23).
    index_record_segments()
    # Сторож работает до последнего действия менеджера (в том числе сторожит
    # само завершение — зависший shutdown ничем не лучше зависшего цикла), и
    # снимается только здесь, чтобы не сработать на процессе, которому
    # осталось выйти.
    if watchdog is not None:
        watchdog.stop()
    logger.info("воркер остановлен")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    time.sleep(5)
    # Не полагаемся на то, что backend уже создал структуру каталогов
    for sub in ("snapshots", "segments", "avatars", "uploads", "thumbs"):
        os.makedirs(os.path.join(MEDIA_PATH, sub), exist_ok=True)
    manager()
