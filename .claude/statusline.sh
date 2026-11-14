#!/usr/bin/env bash
# Status line: модель, ветка, стоимость сессии.
# Claude Code передаёт данные JSON'ом на stdin (переменных окружения
# CLAUDE_MODEL / CLAUDE_COST не существует).

source "${BASH_SOURCE[0]%/*}/hooks/_json.sh"

input=$(cat)
model=$(printf '%s' "$input" | json_get '.model.display_name')
cost=$(printf '%s' "$input" | json_get '.cost.total_cost_usd')
dir=$(printf '%s' "$input" | json_get '.workspace.current_dir')

branch=$(git -C "${dir:-.}" branch --show-current 2>/dev/null)
[ -z "$branch" ] && branch="no-git"

if [ -n "$cost" ]; then
  cost=$(LC_ALL=C awk -v c="$cost" 'BEGIN { printf "$%.2f", c }')
else
  cost="\$0.00"
fi

echo "[${model:-?}] $branch | $cost"
