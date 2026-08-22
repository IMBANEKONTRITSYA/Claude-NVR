FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

# pg_dump нужен «ручному запуску» бэкапа из §11: кнопка в интерфейсе
# снимает дамп процессом бэкенда (backend/app/services/backup.py).
#
# Версия клиента обязана быть НЕ СТАРШЕ сервера — pg_dump 15 против
# сервера 16 отказывается работать вовсе, и узнают об этом в день
# восстановления. В bookworm (база python:3.11-slim) лежит только
# postgresql-client-15, поэтому клиент ставится из официального
# репозитория PostgreSQL: PG_MAJOR обязан совпадать с мажорной версией
# образа postgres в docker-compose.yml, и это сверяется тестом
# backend/tests/test_backup_service.py::test_backend_image_ships_matching_pg_dump.
ARG PG_MAJOR=16
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq-dev gcc ffmpeg curl ca-certificates gnupg \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
      | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update && apt-get install -y --no-install-recommends \
      "postgresql-client-${PG_MAJOR}" \
 && apt-get purge -y curl gnupg && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--log-config", "app/logging_config.json"]
