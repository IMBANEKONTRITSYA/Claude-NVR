"""
FaceWatch worker: читает RTSP-потоки активных камер, делает детекцию движения,
извлекает эмбеддинги лиц через InsightFace, сравнивает с базой персон (pgvector),
публикует события в Redis и пишет видеосегменты при движении/лице.

Минимальная рабочая реализация. Производительность зависит от железа и числа камер.
"""
import os
import sys
import time
import json
import threading
from datetime import datetime, timedelta

import cv2
import numpy as np
import redis
from cryptography.fernet import Fernet
from sqlalchemy import create_engine, select, text, delete
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey, JSON, Text
from pgvector.sqlalchemy import Vector

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
FERNET_KEY = os.environ.get("RTSP_ENCRYPTION_KEY", "ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=")

TARGET_FPS = 5
SIM_THRESHOLD = 0.45  # косинусное расстояние для совпадения
SEGMENT_MAX_SEC = 60

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()
fernet = Fernet(FERNET_KEY.encode())
r = redis.from_url(REDIS_URL, decode_responses=True)


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
    created_at = Column(DateTime)


class FaceEvent(Base):
    __tablename__ = "face_events"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("cameras.id"))
    person_id = Column(Integer, ForeignKey("persons.id"), nullable=True)
    ts = Column(DateTime)
    snapshot_path = Column(String)
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


def update_status(cam_id: int, status: str):
    with Session() as s:
        cam = s.get(Camera, cam_id)
        if cam:
            cam.status = status
            s.commit()
    try:
        r.publish("cameras:status", json.dumps({"camera_id": cam_id, "status": status}))
    except Exception:
        pass


def find_or_create_person(s, emb: np.ndarray) -> tuple[int, bool]:
    res = s.execute(text(
        "SELECT id, 1 - (centroid <=> CAST(:e AS vector)) AS sim FROM persons "
        "WHERE centroid IS NOT NULL ORDER BY centroid <=> CAST(:e AS vector) LIMIT 1"
    ), {"e": str(emb.tolist())}).first()
    if res and res.sim is not None and (1 - res.sim) < SIM_THRESHOLD:
        return res.id, True
    p = Person(name="", status="unknown", centroid=emb.tolist(), created_at=datetime.utcnow())
    s.add(p)
    s.flush()
    return p.id, False


def save_snapshot(frame, cam_id: int, bbox) -> str:
    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1, y1 = max(0, x1), max(0, y1)
    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        crop = frame
    fname = f"cam{cam_id}_{int(time.time()*1000)}.jpg"
    fpath = os.path.join(MEDIA_PATH, "snapshots", fname)
    cv2.imwrite(fpath, crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
    return f"snapshots/{fname}"


def camera_worker(cam_id: int, rtsp_url: str, face_app):
    print(f"[cam {cam_id}] старт {rtsp_url[:40]}...", flush=True)
    cap = cv2.VideoCapture(rtsp_url)
    if not cap.isOpened():
        print(f"[cam {cam_id}] не удалось открыть RTSP", flush=True)
        update_status(cam_id, "offline")
        return
    update_status(cam_id, "online")

    bg = cv2.createBackgroundSubtractorMOG2(history=500, varThreshold=25, detectShadows=False)
    last_proc = 0.0
    interval = 1.0 / TARGET_FPS

    writer = None
    seg_path = None
    seg_started = None
    last_motion = 0.0
    fps_out = 10
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")

    while True:
        ok, frame = cap.read()
        if not ok:
            update_status(cam_id, "offline")
            time.sleep(2)
            cap.release()
            cap = cv2.VideoCapture(rtsp_url)
            continue

        now = time.time()
        if now - last_proc < interval:
            if writer:
                writer.write(frame)
            continue
        last_proc = now

        small = cv2.resize(frame, (640, 360))
        mask = bg.apply(small)
        motion_pixels = int(np.count_nonzero(mask))
        motion = motion_pixels > 1500

        faces = []
        try:
            if motion:
                faces = face_app.get(frame)
        except Exception as e:
            print(f"[cam {cam_id}] ошибка распознавания: {e}", flush=True)
            faces = []

        if motion or faces:
            last_motion = now
            if writer is None:
                seg_started = datetime.utcnow()
                fname = f"cam{cam_id}_{int(time.time())}.mp4"
                seg_path = os.path.join(MEDIA_PATH, "segments", fname)
                h, w = frame.shape[:2]
                writer = cv2.VideoWriter(seg_path, fourcc, fps_out, (w, h))
            writer.write(frame)

        if writer and ((now - last_motion > 5) or (seg_started and (datetime.utcnow() - seg_started).total_seconds() > SEGMENT_MAX_SEC)):
            writer.release()
            try:
                with Session() as s:
                    seg = VideoSegment(
                        camera_id=cam_id,
                        started_at=seg_started,
                        ended_at=datetime.utcnow(),
                        file_path=seg_path,
                        event_type="face" if faces else "motion",
                        duration_sec=int((datetime.utcnow() - seg_started).total_seconds()),
                    )
                    s.add(seg)
                    s.commit()
            except Exception as e:
                print(f"[cam {cam_id}] не удалось сохранить сегмент: {e}", flush=True)
            writer = None
            seg_path = None
            seg_started = None

        if not faces:
            continue

        with Session() as s:
            for f in faces:
                emb = np.asarray(f.normed_embedding, dtype=np.float32)
                if emb.shape[0] != 512:
                    continue
                bbox = f.bbox.tolist()
                pid, matched = find_or_create_person(s, emb)
                person = s.get(Person, pid)
                snap_rel = save_snapshot(frame, cam_id, bbox)
                ev = FaceEvent(
                    camera_id=cam_id,
                    person_id=pid,
                    ts=datetime.utcnow(),
                    snapshot_path=snap_rel,
                    embedding=emb.tolist(),
                    bbox={"x1": bbox[0], "y1": bbox[1], "x2": bbox[2], "y2": bbox[3]},
                    is_known=(person.status == "known"),
                )
                s.add(ev)
                if person.avatar_path is None:
                    person.avatar_path = snap_rel
                s.commit()

                try:
                    r.publish("faces:new", json.dumps({
                        "type": "face",
                        "event_id": ev.id,
                        "camera_id": cam_id,
                        "person_id": pid,
                        "name": person.name or f"Неизвестный #{pid}",
                        "is_known": person.status == "known",
                        "snapshot": snap_rel,
                        "ts": ev.ts.isoformat(),
                    }))
                except Exception:
                    pass


def manager():
    print("[worker] загрузка модели InsightFace...", flush=True)
    face_app = load_face_app()
    print("[worker] модель готова", flush=True)

    threads: dict[int, threading.Thread] = {}
    last_cleanup = time.time()

    while True:
        try:
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

            if time.time() - last_cleanup > 3600:
                last_cleanup = time.time()
                cutoff = datetime.utcnow() - timedelta(days=RETENTION_DAYS)
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
                        p = os.path.join(snap_dir, f)
                        try:
                            if datetime.fromtimestamp(os.path.getmtime(p)) < cutoff:
                                os.remove(p)
                        except Exception:
                            pass

        except Exception as e:
            print(f"[worker] ошибка цикла: {e}", flush=True)
        time.sleep(10)


if __name__ == "__main__":
    time.sleep(5)
    manager()
