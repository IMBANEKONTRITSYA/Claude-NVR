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
# из worker.py (record_encode.py, hwaccel.py, onvif_client.py, onvif_api.py,
# logging_utils.py все по очереди не попадали сюда — контейнер падал в
# CrashLoop с ModuleNotFoundError при первом же реальном docker-сборке,
# незамеченным 17+ циклов аудита, потому что песочница без Docker-демона
# гоняла только pytest с моками, а не настоящий образ). Глоб по всем .py в
# корне контекста сборки исключает этот класс регрессии — новый модуль
# воркера подхватывается автоматически, tests/ не задевается (COPY *.py не
# рекурсивен).
COPY *.py ./

CMD ["python", "worker.py"]
