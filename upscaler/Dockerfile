FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc g++ build-essential libgl1 libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# CPU-сборка torch (на машине пользователя качается с download.pytorch.org)
RUN pip install torch==2.0.1 torchvision==0.15.2 --extra-index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

# Патч известной несовместимости basicsr с новыми torchvision (на 0.15.2 безопасен)
RUN F="$(python -c 'import basicsr,os;print(os.path.dirname(basicsr.__file__))')/data/degradations.py"; \
    sed -i 's/from torchvision.transforms.functional_tensor import/from torchvision.transforms.functional import/' "$F" || true

COPY upscaler.py .

CMD ["python", "upscaler.py"]
