FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ build-essential \
    ffmpeg libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

# Явный список исторически терял модули при каждом выделении нового файла
# из worker.py (hwaccel.py, onvif_client.py, onvif_api.py, record_layer.py,
# logging_utils.py все по очереди не попадали сюда — контейнер падал в
# CrashLoop с ModuleNotFoundError при первом же реальном docker-сборке,
# незамеченным 17+ циклов аудита, потому что песочница без Docker-демона
# гоняла только pytest с моками, а не настоящий образ). Глоб по всем .py в
# корне контекста сборки исключает этот класс регрессии — новый модуль
# воркера подхватывается автоматически, tests/ не задевается (COPY *.py не
# рекурсивен).
# Модель распознавания кладётся В ОБРАЗ, а не качается при первом запуске.
#
# InsightFace по умолчанию тянет её с GitHub в момент первого вызова
# FaceAnalysis(). На production-сервере видеонаблюдения, который обычно
# изолирован от интернета, это гарантированный отказ — и до фикса он ронял
# весь воркер, а с ним слой записи и автообнаружение камер.
#
# Сборка идёт на машине с доступом в сеть, поэтому здесь скачивание
# уместно. Отказ намеренно НЕ роняет сборку: образ без модели остаётся
# рабочим (запись, ONVIF, архив), аналитика включится, когда модель
# появится в томе `insightface-models`.
ARG FACE_MODEL=buffalo_s
RUN python - <<'PY' || echo "МОДЕЛЬ НЕ ВЛОЖЕНА В ОБРАЗ — положите её в том insightface-models вручную"
import os
from insightface.app import FaceAnalysis
name = os.environ.get("FACE_MODEL", "buffalo_s")
FaceAnalysis(name=name, providers=["CPUExecutionProvider"])
print(f"модель {name} вложена в образ")
PY

COPY *.py ./

CMD ["python", "worker.py"]
