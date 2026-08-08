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

from onvif_api import router as onvif_router

# Текст на случай, когда модель не загрузилась: он попадает в интерфейс,
# поэтому называет причину и что делать, а не «ошибка».
MODEL_UNAVAILABLE = (
    "Модель распознавания не загружена — поиск по фото недоступен. "
    "Проверьте журнал воркера: при первом запуске модель скачивается из "
    "интернета, и на изолированном сервере её нужно положить в том "
    "insightface-models вручную."
)


def _enhance_small(img: np.ndarray) -> np.ndarray:
    """Бонус ТЗ: небольшое фото апскейлится перед поиском → выше точность."""
    h, w = img.shape[:2]
    if max(h, w) < 256:
        img = cv2.resize(img, (w * 2, h * 2), interpolation=cv2.INTER_LANCZOS4)
    return img


def build_app(get_face_app) -> FastAPI:
    """`get_face_app` — функция, отдающая текущую модель либо None.

    Именно функция, а не сама модель: API поднимается ДО загрузки модели и
    переживает её отказ. Раньше модель передавалась значением, поэтому
    сервер нельзя было запустить, пока она не загрузится, — а вместе с ним
    недоступным становилось и автообнаружение камер по ONVIF, которое к
    распознаванию лиц отношения не имеет вовсе (SPEC §3).
    """
    app = FastAPI(title="FaceWatch worker embed API")
    app.include_router(onvif_router)

    @app.get("/health")
    async def health():
        # `model_ready` — то, чем бэкенд отличает «воркер не запущен» от
        # «воркер жив, но распознавание недоступно»: снаружи это раньше
        # выглядело одинаково.
        return {"ok": True, "model_ready": get_face_app() is not None}

    @app.post("/embed")
    async def embed(file: UploadFile = File(...)):
        face_app = get_face_app()
        if face_app is None:
            return {"ok": False, "error": MODEL_UNAVAILABLE}
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


def start_embed_api(get_face_app, port: int = 9000):
    app = build_app(get_face_app)
    t = threading.Thread(
        target=lambda: uvicorn.run(app, host="0.0.0.0", port=port, log_level="warning"),
        daemon=True,
    )
    t.start()
    return t
