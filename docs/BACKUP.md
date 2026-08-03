# Резервное копирование и восстановление PostgreSQL

Реализует ТЗ §12 «Резервное копирование»: автоматический ежедневный бэкап,
возможность ручного запуска, документированная процедура восстановления.

## Как это работает

Сервис `backup` в `docker-compose.yml` — официальный образ `postgres:16-alpine`
(та же версия `pg_dump`, что и сервер `postgres`/pgvector) со встроенным
busybox `crond`, без отдельного Dockerfile:

- `backup/entrypoint.sh` записывает cron-задачу по расписанию из
  `BACKUP_SCHEDULE` (по умолчанию `0 3 * * *` — раз в сутки, 03:00) и держит
  контейнер живым (`crond -f`).
- `backup/run.sh` — сам дамп: `pg_dump --clean --if-exists | gzip` в
  `/backups/facewatch_<YYYYMMDD_HHMMSS>.sql.gz`, затем удаляет дампы старше
  `BACKUP_RETENTION_DAYS` (по умолчанию 14 дней).
- Логи бэкапа видны в `docker compose logs backup` (cron job перенаправлен в
  stdout контейнера).

## Ручной запуск

```bash
docker compose exec backup /backup/run.sh
```

На Windows — двойной клик `scripts\backup_now.bat` или та же команда в
PowerShell/cmd.

## Хранение на отдельном диске или сетевом хранилище

По умолчанию дампы лежат в именованном томе Docker (`backups`). Для хранения
на отдельном диске или сетевом хранилище (ТЗ 12) переопределите том в
`docker-compose.override.yml`, не трогая основной файл:

```yaml
services:
  backup:
    volumes:
      - ./backup:/backup:ro
      - D:/facewatch-backups:/backups   # Windows: локальный диск или примонтированная сетевая папка
```

или, для именованного тома с bind-driver на конкретный путь:

```yaml
volumes:
  backups:
    driver: local
    driver_opts:
      type: none
      o: bind
      device: D:/facewatch-backups
```

## Список бэкапов

```bash
docker compose exec backup ls -la /backups
```

## Восстановление из бэкапа

**Перед восстановлением остановите backend и worker**, чтобы они не писали в
БД во время рестора:

```bash
docker compose stop backend worker
```

Восстановление (дамп сделан с `--clean --if-exists`, поэтому пересоздаёт
таблицы/индексы сам — отдельный `DROP DATABASE` не нужен):

```bash
docker compose exec -T backup sh -c 'gunzip -c /backups/facewatch_<TIMESTAMP>.sql.gz' \
  | docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"
```

(На Windows выполняйте эту команду в WSL/Git Bash, либо распакуйте архив
через 7-Zip и передайте в `docker compose exec -T postgres psql ...` через
`type` вместо `gunzip -c`.)

После восстановления:

```bash
docker compose start backend worker
```

Проверьте, что приложение открывается и данные (камеры, персоны, события)
соответствуют ожидаемой точке восстановления.

## Настройка расписания и хранения

В `.env`:

```
BACKUP_SCHEDULE=0 3 * * *
BACKUP_RETENTION_DAYS=14
```

После изменения — `docker compose up -d backup` (пересоздаёт только этот
сервис).
