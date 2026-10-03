#!/usr/bin/env bash
# Поднимает стенд для e2e-опроса: собранный фронтенд + БОЕВЫЕ конфиги
# nginx перед настоящим бэкендом (см. e2e/README.md).
#
# Конфиги nginx берутся из frontend/nginx*.conf теми же подстановками,
# что делает packaging/build-deb.sh: docker-имена хостов → loopback. Это
# принципиально — статические проверки текста конфига (backend/tests/
# test_nginx_*.py) уже есть, а чего у них нет, так это исполнения: цикл 62
# нашёл полностью неработающий пул соединений при зелёном `nginx -t`.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_DIR=${E2E_RUN_DIR:-/tmp/facewatch-e2e}
HTTP_PORT=${E2E_HTTP_PORT:-8080}
BACKEND_ADDR=${E2E_BACKEND_ADDR:-127.0.0.1:8000}
# MediaMTX в песочнице/CI не поднимается: /hls/ проверяется до апстрима —
# на том, что auth_request закрывает его без куки (см. test_ui_live.py).
MEDIAMTX_ADDR=${E2E_MEDIAMTX_ADDR:-127.0.0.1:8888}

stop() {
    pkill -f "uvicorn app.main:app --port ${BACKEND_ADDR##*:}" 2>/dev/null || true
    pkill -f "uvicorn app.main:app" 2>/dev/null || true
    sudo nginx -s stop 2>/dev/null || true
    echo "стенд погашен"
}
[ "${1:-}" = "--stop" ] && { stop; exit 0; }

mkdir -p "$RUN_DIR"/{web,media,backups,logs,certs}

# --- 1. Фронтенд: тот же бандл, что уезжает в образ и в пакет -----------
if [ ! -f "$REPO_ROOT/frontend/dist/index.html" ] || [ "${E2E_REBUILD_FRONTEND:-0}" = "1" ]; then
    ( cd "$REPO_ROOT/frontend" && npm ci --no-audit --no-fund && npm run build )
fi
rm -rf "$RUN_DIR/web"
cp -r "$REPO_ROOT/frontend/dist" "$RUN_DIR/web"
chmod -R a+rX "$RUN_DIR/web"

# --- 2. TLS: nginx.conf объявляет 443-й server{}, без сертификата не стартует
if [ ! -f "$RUN_DIR/certs/fullchain.pem" ]; then
    openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj "/CN=facewatch-e2e" \
        -keyout "$RUN_DIR/certs/privkey.pem" -out "$RUN_DIR/certs/fullchain.pem" 2>/dev/null
fi

# --- 3. nginx из боевых файлов -----------------------------------------
sudo mkdir -p /etc/nginx/snippets /etc/nginx/conf.d
sudo cp "$REPO_ROOT/frontend/nginx-security-headers.conf" \
        /etc/nginx/snippets/nginx-security-headers.conf

sed -e "s#http://facewatch-backend:8000#http://$BACKEND_ADDR#g" \
    -e "s#http://facewatch-mediamtx:8888#http://$MEDIAMTX_ADDR#g" \
    -e "s#^root /usr/share/nginx/html;#root $RUN_DIR/web;#" \
    "$REPO_ROOT/frontend/nginx-locations.conf" \
    | sudo tee /etc/nginx/snippets/nginx-locations.conf >/dev/null

sed -e "s#http://facewatch-backend:8000#http://$BACKEND_ADDR#g" \
    -e "s#server facewatch-backend:8000;#server $BACKEND_ADDR;#g" \
    -e "s#http://facewatch-mediamtx:8888#http://$MEDIAMTX_ADDR#g" \
    -e "s#/etc/nginx/certs/fullchain.pem#$RUN_DIR/certs/fullchain.pem#" \
    -e "s#/etc/nginx/certs/privkey.pem#$RUN_DIR/certs/privkey.pem#" \
    -e "s#access_log /dev/stdout redacted;#access_log $RUN_DIR/logs/nginx-access.log redacted;#" \
    -e "s#^    listen 80;#    listen $HTTP_PORT;#" \
    "$REPO_ROOT/frontend/nginx.conf" | sudo tee /etc/nginx/conf.d/facewatch.conf >/dev/null

# Дефолтный сайт дистрибутива занял бы 80-й порт раньше нашего server{}.
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo nginx -s reload 2>/dev/null || sudo nginx

# --- 4. Бэкенд ----------------------------------------------------------
export DATABASE_URL=${DATABASE_URL:-postgresql+asyncpg://facewatch:facewatch@127.0.0.1:5432/facewatch}

# CREATE EXTENSION vector приложение не делает: в docker это init.sql
# (docker-compose.yml), в пакете — скрипт первого запуска. Без расширения
# lifespan падает на создании HNSW-индексов (§12, поиск по фото).
python3 - <<'PY'
import os, urllib.parse, psycopg2
p = urllib.parse.urlsplit(os.environ["DATABASE_URL"].replace("+asyncpg", ""))
c = psycopg2.connect(host=p.hostname, port=p.port or 5432, user=p.username,
                     password=p.password, dbname=p.path.lstrip("/"), connect_timeout=10)
c.autocommit = True
with c.cursor() as cur:
    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
c.close()
PY
export REDIS_URL=${REDIS_URL:-redis://127.0.0.1:6379/0}
export MEDIA_PATH="$RUN_DIR/media"
export BACKUP_PATH="$RUN_DIR/backups"
export SECRET_KEY=${SECRET_KEY:-e2e-probe-secret-key-long-enough-0123456789abcdef}
export ADMIN_PASSWORD=${ADMIN_PASSWORD:-E2eProbeAdmin!2026}
export RTSP_ENCRYPTION_KEY=${RTSP_ENCRYPTION_KEY:-$(python3 -c 'from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())')}
# Сторож живости валит процесс, когда цикл событий не отмечался дольше
# бюджета; браузерный опрос идёт рывками и даёт ложное срабатывание.
export BACKEND_WATCHDOG_ENABLED=${BACKEND_WATCHDOG_ENABLED:-0}

pkill -f "uvicorn app.main:app" 2>/dev/null || true
# setsid + закрытый stdin: иначе бэкенд остаётся в группе процессов скрипта
# и держит его вывод открытым — вызывающий (шаг CI, конвейер в терминале)
# не дожидается завершения, хотя стенд уже поднят.
( cd "$REPO_ROOT/backend" && setsid python3 -m uvicorn app.main:app \
    --host "${BACKEND_ADDR%%:*}" --port "${BACKEND_ADDR##*:}" \
    < /dev/null > "$RUN_DIR/logs/backend.log" 2>&1 & )

for _ in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "http://$BACKEND_ADDR/api/health" || true)
    [ "$code" = "200" ] && break
    sleep 1
done
[ "${code:-}" = "200" ] || { echo "бэкенд не поднялся"; tail -40 "$RUN_DIR/logs/backend.log"; exit 1; }

curl -sf -o /dev/null "http://127.0.0.1:$HTTP_PORT/api/health"

cat <<EOF
стенд поднят:
  E2E_BASE_URL=http://127.0.0.1:$HTTP_PORT
  E2E_ADMIN_PASSWORD=$ADMIN_PASSWORD
  DATABASE_URL=$DATABASE_URL
  REDIS_URL=$REDIS_URL
EOF
