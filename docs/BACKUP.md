# Резервное копирование и восстановление PostgreSQL

Реализует SPEC §11 «Резервное копирование: автоматическое раз в сутки +
ручной запуск» и строку матрицы прав §18 «Управление бэкапами»
(только администратор).

Способов развёртывания у системы два (§26), и бэкап есть в обоих — но
расписание в них исполняют разные механизмы:

| Режим (§26) | Кто снимает по расписанию | Куда пишет |
|---|---|---|
| Docker Compose (разработка, тесты) | контейнер `backup`, busybox cron | том `backups` → `/backups` |
| .deb + systemd (**production**) | `facewatch-backup.timer` → `facewatch-backup.service` | `/var/lib/facewatch/backups` |

**Ручной запуск в обоих режимах одинаков и делается из интерфейса:**
«Настройки» → «Резервное копирование» → «Создать копию сейчас». Там же
видно, когда снята последняя копия, сколько места занято, и оттуда же
копии скачиваются и удаляются.

Кнопка и таймер зовут **один и тот же код** — `backend/app/services/backup.py`
(таймер через `python -m app.backup_cli`). Две реализации разошлись бы на
первой же правке, и разошлись бы молча: бэкап проверяют в день
восстановления.

## Production: systemd (§26, режим 2)

```bash
systemctl status facewatch-backup.timer     # включён ли, когда следующий запуск
systemctl start facewatch-backup.service    # снять копию прямо сейчас
journalctl -u facewatch-backup -n 50        # что случилось в прошлый раз
ls -la /var/lib/facewatch/backups
```

Каталог, расписание и срок хранения — в `/etc/facewatch/facewatch.env`
(`BACKUP_PATH`, `BACKUP_SCHEDULE`, `BACKUP_RETENTION_DAYS`); фактическое
расписание задаёт `OnCalendar` в юните таймера, а `BACKUP_SCHEDULE`
показывается администратору в интерфейсе. Каталог копий стоит вынести на
диск, отличный от того, где живёт сама СУБД: копия на одном диске с
оригиналом умирает вместе с ним.

Восстановление:

```bash
systemctl stop facewatch-backend facewatch-worker
gunzip -c /var/lib/facewatch/backups/facewatch_<TIMESTAMP>.sql.gz \
  | sudo runuser -u postgres -- psql -d facewatch
systemctl start facewatch-backend facewatch-worker
```

## Docker Compose: как это работает

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

## Ручной запуск без интерфейса

Кнопка «Создать копию сейчас» в «Настройках» — основной способ (он же
работает и в production). Если веб-интерфейс недоступен:

```bash
docker compose exec backup /backup/run.sh          # docker-режим
systemctl start facewatch-backup.service           # production
```

На Windows — двойной клик `scripts\backup_now.bat` или та же команда в
PowerShell/cmd.

## Список бэкапов в интерфейсе

«Настройки» → «Резервное копирование»: возраст последней копии, число
копий и занятое место, скачивание и удаление. Возраст показан отдельным
числом не для красоты — по нему видно, что автоматический бэкап перестал
отрабатывать, пока в каталоге ещё лежат старые копии и всё «выглядит
нормально».

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
