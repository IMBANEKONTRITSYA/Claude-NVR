"""
Внутренний HTTP-API воркера для извлечения эмбеддинга из загруженного фото.
Используется бэкендом для «Поиска похожих лиц». Слушает порт 9000 в сети compose,
наружу не публикуется.
"""
import threading

import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File
import uvicorn


def _enhance_small(img: np.ndarray) -> np.ndarray:
    """Бонус ТЗ: небольшое фото апскейлится перед поиском → выше точность."""
    h, w = img.shape[:2]
    if max(h, w) < 256:
        img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    return img


def build_app(face_app) -> FastAPI:
    app = FastAPI(title="FaceWatch worker embed API")

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/embed")
    async def embed(file: UploadFile = File(...)):
        data = await file.read()
        arr = np.frombuffer(data, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            return {"ok": False, "error": "Не удалось прочитать изображение"}
        img = _enhance_small(img)
        try:
            faces = face_app.get(img)
        except Exception as e:
            return {"ok": False, "error": f"Ошибка детекции: {e}"}
        if not faces:
            return {"ok": False, "error": "Лицо на фото не найдено"}
        # Берём самое крупное лицо
        f = max(faces, key=lambda x: (x.bbox[2] - x.bbox[0]) * (x.bbox[3] - x.bbox[1]))
        emb = np.asarray(f.normed_embedding, dtype=np.float32)
        return {"ok": True, "embedding": emb.tolist()}

    return app


def start_embed_api(face_app, port: int = 9000):
    app = build_app(face_app)
    t = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning"),
        daemon=True,
    )
    t.start()
    return t
