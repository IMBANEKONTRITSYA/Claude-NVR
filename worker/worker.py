"""
FaceWatch worker: для каждой включённой камеры —
- репабликация RTSP в MediaMTX (ffmpeg, copy) → доступно по HLS;
- детекция движения (MOG2) с применением ROI-маски;
- InsightFace эмбеддинги, кластеризация через ближайший центроид (pgvector);
- запись видеосегментов при движении/лице, ротация по RETENTION_DAYS;
- сохранение последнего кадра камеры (snapshots/cam{id}_latest.jpg);
- периодическая DBSCAN-перекластеризация (раз в час) для слияния дублирующихся неизвестных.
"""
import os
import sys
import time
import json
import shlex
import signal
import threading
import subprocess
from datetime import datetime, timedelta

import cv2
import base64
import hashlib
import numpy as np
import redis
from cryptography.fernet import Fernet
from sklearn.cluster import DBSCAN
from sqlalchemy import create_engine, select, text, delete, update
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, JSON, Text
from pgvector.sqlalchemy import Vector

from backoff import reconnect_delay
from shutdown import shutdown_event, handle_shutdown_signal
from logging_utils import configure_logging
import onvif_client

logger = configure_logging("facewatch.worker")

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
FERNET_KEY = os.environ.get("RTSP_ENCRYPTION_KEY", "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=")
MEDIAMTX_HOST = os.environ.get("MEDIAMTX_HOST", "mediamtx")
MEDIAMTX_PORT = int(os.environ.get("MEDIAMTX_PORT", "8554"))

DBSCAN_EPS = 0.35
DBSCAN_MIN_SAMPLES = 3
SEGMENT_MAX_SEC = 60

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
    # Профиль производительности (ТЗ 18)
    "frame_skip": 1,           # анализировать каждый (frame_skip+1)-й обработанный кадр
    "motion_prefilter": 1,     # детектор лиц только по движению
    "idle_fps": 2,             # частота при длительном отсутствии движения
    "face_model": "buffalo_s",
    "upscale_mode": "avatar",  # manual | avatar | all
    "cluster_interval_min": 15,
    "detect_width": 640,
    "record_codec": "h264",
}

# Типы значений настроек: как приводить строку из БД
_CONFIG_TYPES = {
    "retention_days": int, "motion_threshold": int, "similarity_threshold": float,
    "detection_fps": int, "event_cooldown_sec": int, "alert_cooldown_sec": int,
    "telegram_bot_token": str, "telegram_chat_id": str,
    "frame_skip": int, "motion_prefilter": int, "idle_fps": int,
    "face_model": str, "upscale_mode": str, "cluster_interval_min": int,
    "detect_width": int, "record_codec": str,
}

# Секунд без движения, после которых камера уходит в «спящий» режим детекции
IDLE_AFTER_SEC = 20

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
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
                try:
                    CONFIG[row.key] = caster(row.value) if row.value != "" else ("" if caster is str else CONFIG[row.key])
                except (TypeError, ValueError):
                    pass
    except Exception:
        logger.error("не удалось прочитать настройки", exc_info=True)


def send_telegram_alert(person_id: int, name: str, camera_id: int, snapshot_path: str):
    """Telegram-оповещение с cooldown через Redis (один алерт на персону за период)."""
    token = CONFIG["telegram_bot_token"]
    chat = CONFIG["telegram_chat_id"]
    if not token or not chat:
        return
    cooldown_key = f"alert_cooldown:{person_id}"
    try:
        if r.set(cooldown_key, "1", ex=CONFIG["alert_cooldown_sec"], nx=True) is None:
            return  # cooldown активен
    except Exception:
        pass
    text_msg = f"⚠️ FaceWatch: обнаружена персона «{name}» на камере #{camera_id}"
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


class Camera(Base):
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    rtsp_url_enc = Column(Text)
    sub_rtsp_url_enc = Column(Text)      # субпоток для аналитики (ТЗ 18.1)
    location = Column(String)
    enabled = Column(Boolean)
    status = Column(String)
    roi = Column(JSON)
    motion_sensitivity = Column(Integer)
    onvif_enabled = Column(Boolean, default=False)     # ТЗ 18.7
    onvif_host = Column(String)
    onvif_port = Column(Integer)
    onvif_username = Column(String)
    onvif_password_enc = Column(Text)


class Person(Base):
    __tablename__ = "persons"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    status = Column(String)
    avatar_path = Column(String)
    centroid = Column(Vector(512))
    alert_on_detection = Column(Boolean, default=False)
    created_at = Column(DateTime)


class FaceEvent(Base):
    __tablename__ = "face_events"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"))
    person_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    ts = Column(DateTime)
    snapshot_path = Column(String)
    orig_snapshot_path = Column(String)
    enhanced = Column(Boolean, default=False)
    embedding = Column(Vector(512))
    bbox = Column(JSON)
    is_known = Column(Boolean)


class VideoSegment(Base):
    __tablename__ = "video_segments"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"))
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    file_path = Column(String)
    event_type = Column(String)
    duration_sec = Column(Integer)


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


def load_face_app(model_name: str | None = None):
    """Загружает модель детекции/распознавания с учётом профиля (ТЗ 18.5)."""
    global ACCELERATOR, FACE_APP
    from insightface.app import FaceAnalysis
    name = model_name or CONFIG["face_model"]
    providers = detect_providers()
    ACCELERATOR = providers[0].replace("ExecutionProvider", "")
    size = int(CONFIG["detect_width"])
    logger.info("модель загружена", extra={"model": name, "accelerator": ACCELERATOR, "det_size": size})
    app = FaceAnalysis(name=name, providers=providers)
    app.prepare(ctx_id=0, det_size=(size, size))
    FACE_APP = app
    return app


_last_status: dict[int, str] = {}


def update_status(cam_id: int, status: str):
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


def start_republish(cam_id: int, rtsp_url: str) -> subprocess.Popen | None:
    """ffmpeg: TCP-копирование RTSP → MediaMTX (без перекодирования)."""
    out = f"rtsp://{MEDIAMTX_HOST}:{MEDIAMTX_PORT}/cam{cam_id}"
    cmd = [
        "ffmpeg", "-nostdin", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-c", "copy", "-an",
        "-f", "rtsp", "-rtsp_transport", "tcp", out,
    ]
    try:
        return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except FileNotFoundError:
        logger.warning("ffmpeg не найден, репабликация пропущена", extra={"camera_id": cam_id})
        return None


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


def find_or_create_person(s, emb: np.ndarray) -> tuple[int, bool]:
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

    fname = f"cam{cam_id}_{int(time.time() * 1000)}.jpg"
    fpath = os.path.join(MEDIA_PATH, "snapshots", fname)
    if not cv2.imwrite(fpath, crop, [cv2.IMWRITE_JPEG_QUALITY, 85]):
        logger.warning("не удалось записать снимок", extra={"camera_id": cam_id, "path": fpath})
        return None
    return f"snapshots/{fname}"


def save_latest_frame(frame, cam_id: int):
    fpath = os.path.join(MEDIA_PATH, "snapshots", f"cam{cam_id}_latest.jpg")
    cv2.imwrite(fpath, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])


def load_cam_state(cam_id: int) -> tuple[dict | None, bool, int | None]:
    """(roi, active, motion_sensitivity). active=False — камера отключена или удалена."""
    with Session() as s:
        cam = s.get(Camera, cam_id)
        if cam is None or not cam.enabled:
            return None, False, None
        return cam.roi, True, getattr(cam, "motion_sensitivity", None)


def finalize_segment(cam_id: int, tmp_path: str, final_path: str,
                     started, ended, event_type: str):
    """Транскод mp4v → H.264 (браузеры не играют mp4v в <video>) + запись в БД.
    Выполняется в отдельной короткоживущей нити, чтобы не блокировать цикл камеры."""
    ok = False
    # H.265 экономит до 50% места, но не играется в части браузеров —
    # выбор за администратором (ТЗ 18.8).
    codec = "libx265" if CONFIG["record_codec"] == "h265" else "libx264"
    try:
        _lower_priority()
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", tmp_path,
             "-c:v", codec, "-preset", "veryfast", "-crf", "23",
             "-movflags", "+faststart", "-an", final_path],
            timeout=300, check=True,
        )
        os.remove(tmp_path)
        ok = True
    except Exception:
        logger.warning("транскод не удался, оставляю исходник", exc_info=True, extra={"camera_id": cam_id})
        try:
            os.replace(tmp_path, final_path)
            ok = True
        except Exception:
            pass
    if not ok:
        return
    try:
        with Session() as s:
            s.add(VideoSegment(
                camera_id=cam_id,
                started_at=started,
                ended_at=ended,
                file_path=final_path,
                event_type=event_type,
                duration_sec=int((ended - started).total_seconds()),
            ))
            s.commit()
    except Exception:
        logger.error("не удалось сохранить сегмент", exc_info=True, extra={"camera_id": cam_id})


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


def onvif_poll_worker(cam_id: int, host: str, port: int, username: str | None, password: str | None):
    """Фоновая нить на камеру с onvif_enabled: держит PullPoint-подписку и
    складывает события движения в ONVIF_LAST_MOTION/ONVIF_LAST_HEALTHY.
    Никогда не поднимает исключение наружу — рассчитана на постоянный
    daemon-запуск на весь срок жизни камеры, ошибки только логируются и
    ведут к переподписке с экспоненциальной задержкой (тот же backoff, что
    и у RTSP-реконнекта)."""
    attempt = 0
    while not shutdown_event.is_set():
        try:
            subscription_url = onvif_client.create_pull_point_subscription(host, port, username, password)
            logger.info("ONVIF-подписка создана", extra={"camera_id": cam_id, "onvif_host": host})
            attempt = 0
            while not shutdown_event.is_set():
                try:
                    events = onvif_client.pull_messages(subscription_url, username, password)
                except onvif_client.OnvifError:
                    logger.warning("ONVIF pull не удался, переподписка", exc_info=True, extra={"camera_id": cam_id})
                    break
                ONVIF_LAST_HEALTHY[cam_id] = time.time()
                for ev in events:
                    if onvif_client.is_motion_event(ev.get("topic"), ev.get("state")):
                        ONVIF_LAST_MOTION[cam_id] = time.time()
                if shutdown_event.wait(0.5):
                    return
        except onvif_client.OnvifError:
            delay = reconnect_delay(attempt)
            logger.warning(
                "не удалось создать ONVIF-подписку, повтор",
                exc_info=True, extra={"camera_id": cam_id, "retry_in_sec": round(delay, 1)},
            )
            attempt += 1
            if shutdown_event.wait(delay):
                return
        except Exception:
            # Не даём непредвиденной ошибке ONVIF уронить всю нить — это
            # вспомогательный источник детекции, MOG2-фолбэк в camera_worker
            # продолжает работать независимо от состояния этой нити.
            logger.error("непредвиденная ошибка ONVIF-нити", exc_info=True, extra={"camera_id": cam_id})
            if shutdown_event.wait(reconnect_delay(attempt)):
                return
            attempt += 1


def camera_worker(cam_id: int, rtsp_url: str, face_app, sub_rtsp_url: str | None = None,
                   onvif_config: dict | None = None):
    """Двухпоточная схема (ТЗ 18.1): основной поток идёт в архив и HLS,
    аналитика выполняется на субпотоке низкого разрешения, если он задан."""
    analyze_url = sub_rtsp_url or rtsp_url
    logger.info("старт камеры", extra={"camera_id": cam_id, "analytics_stream": "sub" if sub_rtsp_url else "main"})
    republish = start_republish(cam_id, rtsp_url)

    if onvif_config and onvif_config.get("host"):
        threading.Thread(
            target=onvif_poll_worker,
            args=(cam_id, onvif_config["host"], onvif_config.get("port") or 80,
                  onvif_config.get("username"), onvif_config.get("password")),
            daemon=True,
        ).start()

    cap = cv2.VideoCapture(analyze_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        logger.error("не удалось открыть RTSP", extra={"camera_id": cam_id})
        update_status(cam_id, "offline")
        if republish:
            republish.terminate()
        return
    update_status(cam_id, "online")

    bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
    last_proc = 0.0
    last_latest_save = 0.0
    last_state_reload = 0.0
    roi, active, motion_sens = load_cam_state(cam_id)
    roi_mask = None
    roi_mask_shape = None
    frame_counter = 0            # для пропуска кадров (ТЗ 18.3)
    fps_window_start = time.time()
    fps_frames = 0

    writer = None
    seg_path = None
    seg_tmp = None
    seg_started = None
    seg_had_face = False   # было ли лицо хоть в одном кадре сегмента
    last_event_at: dict[int, float] = {}  # person_id -> время последнего события (тротлинг)
    last_motion = 0.0
    # В сегмент пишутся ВСЕ кадры потока, поэтому fps контейнера должен
    # совпадать с fps камеры — иначе видео играет с неверной скоростью
    # (с жёстким fps=10 запись с 25-fps камеры шла в 2.5 раза медленнее).
    cam_fps = cap.get(cv2.CAP_PROP_FPS)
    fps_out = cam_fps if (cam_fps and 1.0 <= cam_fps <= 60.0) else 25.0
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    def close_segment(reason_ended):
        nonlocal writer, seg_path, seg_tmp, seg_started, seg_had_face
        writer.release()
        etype = "face" if seg_had_face else "motion"
        threading.Thread(
            target=finalize_segment,
            args=(cam_id, seg_tmp, seg_path, seg_started, reason_ended, etype),
            daemon=True,
        ).start()
        writer = None
        seg_path = None
        seg_tmp = None
        seg_started = None
        seg_had_face = False

    reconnect_attempt = 0
    while True:
        if shutdown_event.is_set():
            logger.info("остановка (shutdown), освобождаю ресурсы", extra={"camera_id": cam_id})
            if writer:
                close_segment(datetime.utcnow())
            cap.release()
            if republish:
                republish.terminate()
            update_status(cam_id, "offline")
            return

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
            cap = cv2.VideoCapture(analyze_url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                update_status(cam_id, "online")
            continue
        if reconnect_attempt:
            reconnect_attempt = 0

        # Авто-перезапуск ffmpeg-репабликации, если он умер
        if republish and republish.poll() is not None:
            logger.warning("ffmpeg-репабликация упала, перезапускаю", extra={"camera_id": cam_id})
            republish = start_republish(cam_id, rtsp_url)

        now = time.time()
        # Адаптивная частота (ТЗ 18.3): при длительном покое опускаемся до idle_fps,
        # при первом же движении мгновенно возвращаемся к полной частоте.
        idle = (now - last_motion) > IDLE_AFTER_SEC
        target_fps = CONFIG["idle_fps"] if idle else CONFIG["detection_fps"]
        interval = 1.0 / max(1, target_fps)
        if now - last_proc < interval:
            if writer:
                writer.write(frame)
            continue
        last_proc = now

        # Пропуск кадров: анализируем каждый (frame_skip+1)-й отобранный кадр
        frame_counter += 1
        skip = int(CONFIG["frame_skip"])
        if skip and (frame_counter % (skip + 1)) != 0:
            if writer:
                writer.write(frame)
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
            roi, active, motion_sens = load_cam_state(cam_id)
            roi_mask = None
            last_state_reload = now
            if not active:
                # Камера отключена или удалена — корректно останавливаем поток
                logger.info("камера отключена, останавливаю обработку", extra={"camera_id": cam_id})
                if writer:
                    close_segment(datetime.utcnow())
                cap.release()
                if republish:
                    republish.terminate()
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

        if motion or faces:
            last_motion = now
            if writer is None:
                seg_started = datetime.utcnow()
                fname = f"cam{cam_id}_{int(time.time())}.mp4"
                seg_path = os.path.join(MEDIA_PATH, "segments", fname)
                seg_tmp = seg_path.replace(".mp4", "_tmp.mp4")
                seg_had_face = False
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(seg_tmp, fourcc, fps_out, (w, h))
            if faces:
                seg_had_face = True
            writer.write(frame)

        if writer and (
            (now - last_motion > 5)
            or (seg_started and (datetime.utcnow() - seg_started).total_seconds() > SEGMENT_MAX_SEC)
        ):
            close_segment(datetime.utcnow())

        if not faces:
            continue

        fh, fw = frame.shape[:2]
        try:
            process_faces(cam_id, frame, faces, fw, fh, now, last_event_at)
        except Exception:
            # Любой сбой на одном кадре не должен убивать нить камеры
            logger.error("ошибка обработки лиц", exc_info=True, extra={"camera_id": cam_id})


def process_faces(cam_id, frame, faces, fw, fh, now, last_event_at):
    with Session() as s:
        for f in faces:
            emb = np.asarray(f.normed_embedding, dtype=np.float32)
            if emb.shape[0] != 512:
                continue
            bbox = f.bbox.tolist()
            pid, matched = find_or_create_person(s, emb)
            person = s.get(Person, pid)
            s.commit()  # фиксируем возможную новую персону сразу

            name = person.name or f"Неизвестный #{pid}"
            is_known = person.status == "known"
            bbox_json = {"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]}

            # Watchlist-оповещение: свой redis-cooldown, шлём из фоновой нити,
            # чтобы HTTP к Telegram (до 5с) не тормозил обработку кадров
            if is_known and getattr(person, "alert_on_detection", False):
                threading.Thread(
                    target=send_telegram_alert,
                    args=(pid, person.name or f"#{pid}", cam_id, ""),
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

            snap_rel = save_face_snapshot(frame, cam_id, bbox)
            ev = FaceEvent(
                camera_id=cam_id,
                person_id=pid,
                ts=datetime.utcnow(),
                snapshot_path=snap_rel,
                orig_snapshot_path=snap_rel,
                enhanced=False,
                embedding=emb.tolist(),
                bbox=bbox_json,
                is_known=is_known,
            )
            s.add(ev)
            is_new_avatar = person.avatar_path is None and bool(snap_rel)
            if is_new_avatar:
                person.avatar_path = snap_rel
            s.commit()

            try:
                r.publish("faces:new", json.dumps({
                    "type": "face",
                    "event_id": ev.id,
                    "camera_id": cam_id,
                    "person_id": pid,
                    "name": name,
                    "is_known": is_known,
                    "snapshot": snap_rel,
                    "ts": ev.ts.isoformat(),
                    "bbox": bbox_json,
                    "frame_w": fw,
                    "frame_h": fh,
                }))
                # Ленивый апскейл (ТЗ 18.6): в режиме "manual" не делаем ничего,
                # в "avatar" улучшаем только первый кадр персоны (её аватар),
                # в "all" — всю галерею. Очередь ограничена, чтобы медленный
                # CPU-апскейл не копил бэклог часами.
                mode = CONFIG["upscale_mode"]
                want = mode == "all" or (mode == "avatar" and is_new_avatar)
                if want and r.llen("upscale:queue") < 500:
                    r.lpush("upscale:queue", json.dumps({"event_id": ev.id}))
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


def cleanup_old():
    cutoff = datetime.utcnow() - timedelta(days=CONFIG["retention_days"])
    with Session() as s:
        old = s.execute(select(VideoSegment).where(VideoSegment.started_at < cutoff)).scalars().all()
        for seg in old:
            try:
                if seg.file_path and os.path.exists(seg.file_path):
                    os.remove(seg.file_path)
            except Exception:
                pass
            s.delete(seg)
        s.execute(delete(FaceEvent).where(FaceEvent.ts < cutoff))
        s.commit()
    snap_dir = os.path.join(MEDIA_PATH, "snapshots")
    if os.path.isdir(snap_dir):
        for f in os.listdir(snap_dir):
            if f.endswith("_latest.jpg"):
                continue
            p = os.path.join(snap_dir, f)
            try:
                if datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    # Осиротевшие временные сегменты (например, после падения процесса)
    seg_dir = os.path.join(MEDIA_PATH, "segments")
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    if os.path.isdir(seg_dir):
        for f in os.listdir(seg_dir):
            if not f.endswith("_tmp.mp4"):
                continue
            p = os.path.join(seg_dir, f)
            try:
                if datetime.fromtimestamp(os.path.getmtime(p)) < hour_ago:
                    os.remove(p)
            except Exception:
                pass


def manager():
    # Настройки читаем ДО загрузки модели: профиль задаёт face_model и
    # detect_width, иначе выбор в админке не применялся бы до перезапуска.
    refresh_config()
    logger.info("загрузка модели InsightFace...")
    face_app = load_face_app()
    logger.info("модель готова")
    loaded_model = (CONFIG["face_model"], CONFIG["detect_width"])

    # Внутренний HTTP-API для извлечения эмбеддинга (поиск по фото)
    try:
        from embed_api import start_embed_api
        start_embed_api(face_app, port=9000)
        logger.info("embed-API запущен", extra={"port": 9000})
    except Exception:
        logger.error("не удалось запустить embed-API", exc_info=True)

    threads: dict[int, threading.Thread] = {}
    last_cleanup = 0.0
    last_recluster = 0.0

    logger.info("конфиг воркера", extra={"config": CONFIG})

    while not shutdown_event.is_set():
        try:
            refresh_config()
            # Смена модели/разрешения в профиле применяется без перезапуска
            if (CONFIG["face_model"], CONFIG["detect_width"]) != loaded_model:
                logger.info("параметры модели изменились, перезагружаю")
                try:
                    load_face_app()  # обновляет глобальный FACE_APP
                    loaded_model = (CONFIG["face_model"], CONFIG["detect_width"])
                except Exception:
                    logger.error("не удалось сменить модель", exc_info=True)
            with Session() as s:
                cams = s.execute(select(Camera).where(Camera.enabled == True)).scalars().all()
                for cam in cams:
                    if cam.id in threads and threads[cam.id].is_alive():
                        continue
                    try:
                        rtsp = fernet.decrypt(cam.rtsp_url_enc.encode()).decode()
                        sub_enc = getattr(cam, "sub_rtsp_url_enc", None)
                        sub = fernet.decrypt(sub_enc.encode()).decode() if sub_enc else None
                    except Exception:
                        logger.error("не удалось расшифровать RTSP", exc_info=True, extra={"camera_id": cam.id})
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
                    t = threading.Thread(
                        target=camera_worker, args=(cam.id, rtsp, face_app, sub, onvif_config), daemon=True
                    )
                    t.start()
                    threads[cam.id] = t

            now = time.time()
            if now - last_cleanup > 3600:
                last_cleanup = now
                try:
                    cleanup_old()
                except Exception:
                    logger.error("ошибка очистки (cleanup)", exc_info=True)

            # Пакетная кластеризация (ТЗ 18.6): интервал задаётся профилем,
            # выполняется в фоновой нити с пониженным приоритетом.
            if now - last_recluster > CONFIG["cluster_interval_min"] * 60:
                last_recluster = now
                threading.Thread(target=_recluster_bg, daemon=True).start()

        except Exception:
            logger.error("ошибка цикла воркера", exc_info=True)
        # Прерываемое ожидание: shutdown не должен ждать до 10с впустую.
        shutdown_event.wait(10)

    logger.info("завершение: жду остановки нитей камер...")
    # Бюджет ожидания общий на все камеры (не по 8с на каждую), иначе
    # остановка 16 камер могла бы растянуться на пару минут и упереться
    # в SIGKILL раньше, чем нити успеют освободить ресурсы.
    deadline = time.time() + 8.0
    for cam_id, t in threads.items():
        t.join(timeout=max(0.0, deadline - time.time()))
        if t.is_alive():
            logger.warning("нить камеры не успела остановиться в срок", extra={"camera_id": cam_id})
    logger.info("воркер остановлен")


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)
    time.sleep(5)
    # Не полагаемся на то, что backend уже создал структуру каталогов
    for sub in ("snapshots", "segments", "avatars", "uploads"):
        os.makedirs(os.path.join(MEDIA_PATH, sub), exist_ok=True)
    manager()
