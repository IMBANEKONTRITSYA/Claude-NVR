#!/bin/sh
# Настраивает cron на образе postgres:16-alpine (busybox crond уже есть,
# отдельный образ не нужен) и держит контейнер живым в foreground.
set -eu

chmod +x /backup/run.sh

schedule="${BACKUP_SCHEDULE:-0 3 * * *}"
# Вывод джобы редиректим в stdout процесса crond (pid 1) — иначе busybox
# crond по умолчанию проглатывает stdout/stderr задач, и `docker logs` был
# бы пустым.
echo "${schedule} /backup/run.sh >>/proc/1/fd/1 2>>/proc/1/fd/2" > /etc/crontabs/root

echo "[backup] расписание бэкапа: ${schedule} (retention: ${BACKUP_RETENTION_DAYS:-14} дн.)"
exec crond -f -l 2
