#!/usr/bin/env bash
# SessionStart: stdout этого хука добавляется в контекст сессии.

cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

echo "=== Контекст сессии FaceWatch ==="
echo "Ветка: $(git branch --show-current 2>/dev/null || echo 'не в git')"
echo "Рабочая база для PR: claude/pensive-brown-28588t (main — стабильная копия; PR с head = рабочая ветка не открывать)"
echo "Изменённых файлов: $(git status --short 2>/dev/null | wc -l | tr -d ' ')"
echo "Последний коммит: $(git log -1 --pretty=format:'%h %s' 2>/dev/null || echo 'нет')"

# Docker может не отвечать (не запущен Docker Desktop) — не ждём дольше 5 с.
if command -v docker >/dev/null 2>&1; then
  if command -v timeout >/dev/null 2>&1; then t="timeout 5"; else t=""; fi
  services=$($t docker compose ps --services --status running 2>/dev/null)
  if [ -n "$services" ]; then
    echo "Docker-сервисы запущены: $(echo "$services" | tr '\n' ' ')"
  else
    echo "Docker-сервисы: не запущены"
  fi
fi

echo "================================="
exit 0
