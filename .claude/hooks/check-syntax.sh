#!/usr/bin/env bash
# PostToolUse (Edit|Write|MultiEdit): быстрая проверка синтаксиса правленого файла.
#
# Автоформатирование (black/prettier) намеренно НЕ делается: в проекте нет
# ни black, ни prettier, код ими не отформатирован, и прогон по файлу целиком
# превращал бы правку одной строки в дифф на весь файл.
# Код выхода 2 — stderr уходит модели, и она сразу исправляет ошибку.

source "${BASH_SOURCE[0]%/*}/_json.sh"

file=$(json_get '.tool_input.file_path')
[ -z "$file" ] || [ ! -f "$file" ] && exit 0

case "$file" in
  *.py)
    py=$(command -v python3 || command -v python) || exit 0
    if ! out=$("$py" -m py_compile "$file" 2>&1); then
      echo "Синтаксическая ошибка Python в $file:" >&2
      echo "$out" >&2
      exit 2
    fi
    ;;
  *.json)
    py=$(command -v python3 || command -v python) || exit 0
    if ! out=$("$py" -c 'import json,sys; json.load(open(sys.argv[1], encoding="utf-8"))' "$file" 2>&1); then
      echo "Невалидный JSON в $file:" >&2
      echo "$out" >&2
      exit 2
    fi
    ;;
  *.sh)
    if ! out=$(bash -n "$file" 2>&1); then
      echo "Синтаксическая ошибка shell в $file:" >&2
      echo "$out" >&2
      exit 2
    fi
    ;;
esac

# TypeScript здесь не проверяется: tsc на один файл без проекта даёт ложные
# ошибки, а `npm run build` слишком долгий для хука — запускай его сам.
exit 0
