#!/usr/bin/env bash
# Общий помощник для хуков: достать поле из JSON на stdin.
# jq есть не везде (Git Bash под Windows), поэтому запасной путь — python.
# Использование: value=$(printf '%s' "$input" | json_get '.tool_input.file_path')

json_get() {
  local path="$1"
  if command -v jq >/dev/null 2>&1; then
    jq -r "$path // empty" 2>/dev/null
    return
  fi
  local py
  py=$(command -v python3 || command -v python) || return 0
  "$py" -c '
import json, sys
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for key in sys.argv[1].lstrip(".").split("."):
    data = data.get(key) if isinstance(data, dict) else None
    if data is None:
        sys.exit(0)
print(data)
' "$path"
}
