#!/bin/sh
# Настраивает cron на образе postgres:16-alpine (busybox crond уже есть,
# отдельный образ не нужен) и держит контейнер живым в foreground.
set -eu

# ./backup смонтирован как :ro в docker-compose.yml (сам скрипт не должен
# модифицироваться из контейнера) — chmod на read-only bind-mount всегда
# возвращает "Read-only file system", а из-за `set -eu` это раньше валило
# весь entrypoint при каждом старте контейнера (бэкап не работал в принципе,
# не только на Windows). run.sh уже имеет +x в самом репозитории (режим
# 755, `git ls-files -s`), так что chmod здесь не нужен для штатного случая
# — оставлен best-effort на случай чекаута, теряющего exec-бит (например,
# распаковка из zip-архива вместо git clone), но больше не должен уронить
# запуск, если файл и так неперезаписываем.
chmod +x /backup/run.sh 2>/dev/null || true

schedule="${BACKUP_SCHEDULE:-0 3 * * *}"
# Вывод джобы редиректим в stdout процесса crond (pid 1) — иначе busybox
# crond по умолчанию проглатывает stdout/stderr задач, и `docker logs` был
# бы пустым.
echo "${schedule} /backup/run.sh >>/proc/1/fd/1 2>>/proc/1/fd/2" > /etc/crontabs/root

echo "[backup] расписание бэкапа: ${schedule} (retention: ${BACKUP_RETENTION_DAYS:-14} дн.)"
exec crond -f -l 2
