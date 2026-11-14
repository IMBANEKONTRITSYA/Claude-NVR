#!/bin/sh
# Снимает дамп PostgreSQL в /backups и удаляет дампы старше BACKUP_RETENTION_DAYS.
# Запускается по расписанию из entrypoint.sh (cron) или вручную:
#   docker compose exec backup /backup/run.sh
set -eu

ts=$(date +%Y%m%d_%H%M%S)
out="/backups/facewatch_${ts}.sql.gz"
retention="${BACKUP_RETENTION_DAYS:-14}"

export PGPASSWORD="$POSTGRES_PASSWORD"

echo "[backup] старт: ${out}"
# --clean --if-exists: дамп можно накатить обратно тем же psql-пайпом даже
# на уже существующую БД (пересоздаёт объекты), без ручного DROP DATABASE.
pg_dump -h "$POSTGRES_HOST" -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists \
  | gzip > "$out"
size=$(du -h "$out" | cut -f1)
echo "[backup] готово: ${out} (${size})"

deleted=$(find /backups -name 'facewatch_*.sql.gz' -mtime "+${retention}" -print -delete | wc -l)
if [ "$deleted" -gt 0 ]; then
  echo "[backup] удалено старых дампов (старше ${retention} дн.): ${deleted}"
fi
