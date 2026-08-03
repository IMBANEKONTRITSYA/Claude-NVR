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
import threading
import subprocess
import traceback
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
    "detection_fps": 5,
    "event_cooldown_sec": 10,
    "alert_cooldown_sec": 300,
    "telegram_bot_token": "",
    "telegram_chat_id": "",
}

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
            rows = s.execute(select(Setting)).scalars().all()
            for row in rows:
                if row.key == "retention_days":
                    CONFIG["retention_days"] = int(row.value)
                elif row.key == "motion_threshold":
                    CONFIG["motion_threshold"] = int(row.value)
                elif row.key == "similarity_threshold":
                    CONFIG["similarity_threshold"] = float(row.value)
                elif row.key == "detection_fps":
                    CONFIG["detection_fps"] = int(row.value)
                elif row.key == "event_cooldown_sec":
                    CONFIG["event_cooldown_sec"] = int(row.value)
                elif row.key == "alert_cooldown_sec":
                    CONFIG["alert_cooldown_sec"] = int(row.value)
                elif row.key == "telegram_bot_token":
                    CONFIG["telegram_bot_token"] = row.value or ""
                elif row.key == "telegram_chat_id":
                    CONFIG["telegram_chat_id"] = row.value or ""
    except Exception as e:
        print(f"[worker] не удалось прочитать настройки: {e}", flush=True)


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
    except Exception as e:
        print(f"[alert] telegram error: {e}", flush=True)


class Camera(Base):
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True)
    name = Column(String)
    rtsp_url_enc = Column(Text)
    location = Column(String)
    enabled = Column(Boolean)
    status = Column(String)
    roi = Column(JSON)


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


def load_face_app():
    from insightface.app import FaceAnalysis
    app = FaceAnalysis(name="buffalo_s", providers=["CPUExecutionProvider"])
    app.prepare(ctx_id=0, det_size=(640, 640))
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
        print(f"[cam {cam_id}] ffmpeg не найден, репабликация пропущена", flush=True)
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
        print(f"[cam {cam_id}] не удалось записать снимок {fpath}", flush=True)
        return None
    return f"snapshots/{fname}"


def save_latest_frame(frame, cam_id: int):
    fpath = os.path.join(MEDIA_PATH, "snapshots", f"cam{cam_id}_latest.jpg")
    cv2.imwrite(fpath, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])


def load_cam_state(cam_id: int) -> tuple[dict | None, bool]:
    """(roi, active). active=False — камера удалена или отключена: поток должен завершиться."""
    with Session() as s:
        cam = s.get(Camera, cam_id)
        if cam is None or not cam.enabled:
            return None, False
        return cam.roi, True


def finalize_segment(cam_id: int, tmp_path: str, final_path: str,
                     started, ended, event_type: str):
    """Транскод mp4v → H.264 (браузеры не играют mp4v в <video>) + запись в БД.
    Выполняется в отдельной короткоживущей нити, чтобы не блокировать цикл камеры."""
    ok = False
    try:
        subprocess.run(
            ["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-i", tmp_path,
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-movflags", "+faststart", "-an", final_path],
            timeout=300, check=True,
        )
        os.remove(tmp_path)
        ok = True
    except Exception as e:
        print(f"[cam {cam_id}] транскод не удался ({e}), оставляю исходник", flush=True)
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
    except Exception as e:
        print(f"[cam {cam_id}] не удалось сохранить сегмент: {e}", flush=True)


def camera_worker(cam_id: int, rtsp_url: str, face_app):
    print(f"[cam {cam_id}] старт", flush=True)
    republish = start_republish(cam_id, rtsp_url)

    cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
    if not cap.isOpened():
        print(f"[cam {cam_id}] не удалось открыть RTSP", flush=True)
        update_status(cam_id, "offline")
        if republish:
            republish.terminate()
        return
    update_status(cam_id, "online")

    bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
    last_proc = 0.0
    last_latest_save = 0.0
    last_state_reload = 0.0
    roi, active = load_cam_state(cam_id)
    roi_mask = None
    roi_mask_shape = None

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

    while True:
        ok, frame = cap.read()
        if not ok:
            update_status(cam_id, "offline")
            time.sleep(2)
            cap.release()
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            if cap.isOpened():
                update_status(cam_id, "online")
            continue

        # Авто-перезапуск ffmpeg-репабликации, если он умер
        if republish and republish.poll() is not None:
            print(f"[cam {cam_id}] ffmpeg-репабликация упала, перезапускаю", flush=True)
            republish = start_republish(cam_id, rtsp_url)

        now = time.time()
        interval = 1.0 / max(1, CONFIG["detection_fps"])
        if now - last_proc < interval:
            if writer:
                writer.write(frame)
            continue
        last_proc = now

        if now - last_latest_save > 2.0:
            save_latest_frame(frame, cam_id)
            last_latest_save = now

        if now - last_state_reload > 10.0:
            roi, active = load_cam_state(cam_id)
            roi_mask = None
            last_state_reload = now
            if not active:
                # Камера отключена или удалена — корректно останавливаем поток
                print(f"[cam {cam_id}] отключена, останавливаю обработку", flush=True)
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

        small = cv2.resize(frame, (640, 360))
        fg = bg.apply(small)
        if roi_mask is not None:
            small_mask = cv2.resize(roi_mask, (640, 360), interpolation=cv2.INTER_NEAREST)
            fg = cv2.bitwise_and(fg, fg, mask=small_mask)
        motion_pixels = int(np.count_nonzero(fg))
        motion = motion_pixels > CONFIG["motion_threshold"]

        faces = []
        try:
            if motion:
                detected = face_app.get(frame)
                faces = [f for f in detected if bbox_in_roi(f.bbox.tolist(), roi_mask)]
        except Exception as e:
            print(f"[cam {cam_id}] ошибка распознавания: {e}", flush=True)
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
            print(f"[cam {cam_id}] ошибка обработки лиц:\n{traceback.format_exc()}", flush=True)


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
            if person.avatar_path is None and snap_rel:
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
                # Ставим скриншот в очередь на нейросетевой апскейл (асинхронно).
                # Ограничиваем очередь, чтобы медленный CPU-апскейл не копил бэклог часами.
                if r.llen("upscale:queue") < 500:
                    r.lpush("upscale:queue", json.dumps({"event_id": ev.id}))
            except Exception:
                pass


def _to_vec(val) -> np.ndarray:
    """pgvector через psycopg2 без register_vector возвращает строку '[a,b,...]';
    через ORM — список. Приводим оба варианта к np.ndarray."""
    if isinstance(val, str):
        return np.fromstring(val.strip("[]"), sep=",", dtype=np.float32)
    return np.asarray(val, dtype=np.float32)


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
            print(f"[recluster] неожиданная форма эмбеддингов: {embs.shape}", flush=True)
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
            print(f"[recluster] объединено персон: {merged}", flush=True)


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
    print("[worker] загрузка модели InsightFace...", flush=True)
    face_app = load_face_app()
    print("[worker] модель готова", flush=True)

    # Внутренний HTTP-API для извлечения эмбеддинга (поиск по фото)
    try:
        from embed_api import start_embed_api
        start_embed_api(face_app, port=9000)
        print("[worker] embed-API запущен на :9000", flush=True)
    except Exception as e:
        print(f"[worker] не удалось запустить embed-API: {e}", flush=True)

    threads: dict[int, threading.Thread] = {}
    last_cleanup = 0.0
    last_recluster = 0.0

    refresh_config()
    print(f"[worker] конфиг: {CONFIG}", flush=True)

    while True:
        try:
            refresh_config()
            with Session() as s:
                cams = s.execute(select(Camera).where(Camera.enabled == True)).scalars().all()
                for cam in cams:
                    if cam.id in threads and threads[cam.id].is_alive():
                        continue
                    try:
                        rtsp = fernet.decrypt(cam.rtsp_url_enc.encode()).decode()
                    except Exception as e:
                        print(f"[cam {cam.id}] не удалось расшифровать RTSP: {e}", flush=True)
                        continue
                    t = threading.Thread(target=camera_worker, args=(cam.id, rtsp, face_app), daemon=True)
                    t.start()
                    threads[cam.id] = t

            now = time.time()
            if now - last_cleanup > 3600:
                last_cleanup = now
                try:
                    cleanup_old()
                except Exception as e:
                    print(f"[worker] cleanup ошибка: {e}", flush=True)

            if now - last_recluster > 3600:
                last_recluster = now
                try:
                    recluster_unknowns()
                except Exception as e:
                    print(f"[worker] recluster ошибка: {e}", flush=True)

        except Exception as e:
            print(f"[worker] ошибка цикла: {e}", flush=True)
        time.sleep(10)


if __name__ == "__main__":
    time.sleep(5)
    # Не полагаемся на то, что backend уже создал структуру каталогов
    for sub in ("snapshots", "segments", "avatars", "uploads"):
        os.makedirs(os.path.join(MEDIA_PATH, sub), exist_ok=True)
    manager()
