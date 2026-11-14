#!/usr/bin/env bash
# PreToolUse (Edit|Write|MultiEdit): блокирует изменение защищённых файлов.
# Код выхода 2 — Claude Code отменяет вызов и показывает stderr модели.

source "${BASH_SOURCE[0]%/*}/_json.sh"

file=$(json_get '.tool_input.file_path')
[ -z "$file" ] && exit 0

# Путь относительно корня проекта: иначе подстрока вроде "media/" совпала бы
# с любым абсолютным путём, где проект лежит, например, в /mnt/media/...
root="${CLAUDE_PROJECT_DIR:-$PWD}"
file="${file//\\//}"
root="${root//\\//}"
rel="${file#"$root"/}"
name="${rel##*/}"

block() {
  echo "Файл '$rel' защищён ($1). Не изменяй его без явной просьбы пользователя." >&2
  exit 2
}

# .env и его локальные варианты — да; .env.example — нет, его как раз правят.
case "$name" in
  .env.example) ;;
  .env|.env.*) block "секреты окружения" ;;
  *.pem|*.key|*.crt) block "ключи/сертификаты" ;;
esac

case "$rel" in
  secrets/*|*/secrets/*)        block "каталог секретов" ;;
  .local/*)                     block "локальные учётки и пробы развёртывания" ;;
  backup/data/*)                block "дампы БД" ;;
  media/*)                      block "архив и снимки" ;;
  *node_modules/*|*__pycache__/*|*.venv/*|*/dist/*) block "сгенерированные файлы" ;;
esac

exit 0
