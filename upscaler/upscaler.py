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
import time
import json

import cv2
import numpy as np
import redis
from sqlalchemy import create_engine, Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.environ["DATABASE_URL"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
MEDIA_PATH = os.environ.get("MEDIA_PATH", "/media")
UPSCALE_BACKEND = os.environ.get("UPSCALE_BACKEND", "gfpgan")  # gfpgan|opencv
GFPGAN_MODEL_URL = os.environ.get(
    "GFPGAN_MODEL_URL",
    "https://github.com/TencentARC/GFPGAN/releases/download/v1.3.4/GFPGANv1.4.pth",
)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)
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
        except Exception as e:
            print(f"[upscaler] GFPGAN недоступен ({e}); fallback на OpenCV", flush=True)
    return enhance_opencv(img), "opencv"


# ---------------------------------------------------------------------------

def abspath(rel: str) -> str:
    return os.path.join(MEDIA_PATH, rel)


def process_event(event_id: int):
    with Session() as s:
        ev = s.get(FaceEvent, event_id)
        if not ev:
            return
        src_rel = ev.orig_snapshot_path or ev.snapshot_path
        if not src_rel:
            return
        src = abspath(src_rel)
        if not os.path.exists(src):
            print(f"[upscaler] нет файла {src}", flush=True)
            return
        img = cv2.imread(src)
        if img is None:
            return

        out_img, backend = enhance(img)

        name = os.path.basename(src_rel)
        enh_rel = f"snapshots/enh_{name}"
        cv2.imwrite(abspath(enh_rel), out_img, [cv2.IMWRITE_JPEG_QUALITY, 92])

        prev_snapshot = ev.snapshot_path
        ev.snapshot_path = enh_rel
        ev.enhanced = True

        # Обновляем аватар персоны, если он указывал на исходный/прежний снимок
        person = s.get(Person, ev.person_id) if ev.person_id else None
        if person and person.avatar_path in (src_rel, prev_snapshot, None):
            person.avatar_path = enh_rel
        s.commit()

        try:
            r.publish("faces:enhanced", json.dumps({
                "type": "enhanced",
                "event_id": ev.id,
                "person_id": ev.person_id,
                "snapshot": enh_rel,
                "backend": backend,
            }))
        except Exception:
            pass
        print(f"[upscaler] событие {event_id} улучшено ({backend})", flush=True)


def main():
    print(f"[upscaler] старт, backend={UPSCALE_BACKEND}", flush=True)
    if UPSCALE_BACKEND == "gfpgan":
        try:
            _load_gfpgan()
            print("[upscaler] модель GFPGAN загружена", flush=True)
        except Exception as e:
            print(f"[upscaler] не удалось загрузить GFPGAN заранее ({e})", flush=True)
    while True:
        try:
            item = r.blpop("upscale:queue", timeout=5)
            if not item:
                continue
            payload = json.loads(item[1])
            eid = payload.get("event_id")
            if eid is not None:
                process_event(int(eid))
        except Exception as e:
            print(f"[upscaler] ошибка: {e}", flush=True)
            time.sleep(1)


if __name__ == "__main__":
    time.sleep(8)
    main()
