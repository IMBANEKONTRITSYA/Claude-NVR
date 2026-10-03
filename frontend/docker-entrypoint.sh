#!/bin/sh
# ТЗ 13: "полное шифрование данных на уровне приложения при передаче по
# сети (TLS/HTTPS) — даже внутри локальной сети рекомендуется". Генерирует
# самоподписанный сертификат при первом запуске, если его ещё нет в
# смонтированном томе /etc/nginx/certs (переживает пересоздание контейнера,
# поэтому браузеру не придётся заново доверять новому сертификату при
# каждом перезапуске).
set -eu

CERT_DIR=/etc/nginx/certs
CERT_FILE="$CERT_DIR/fullchain.pem"
KEY_FILE="$CERT_DIR/privkey.pem"

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    mkdir -p "$CERT_DIR"
    echo "[tls] сертификат не найден, генерирую самоподписанный (действителен 10 лет)..."
    openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -subj "/CN=facewatch.local" \
        -addext "subjectAltName=DNS:localhost,DNS:facewatch.local,IP:127.0.0.1" \
        2>&1 | sed 's/^/[tls] /'
    chmod 600 "$KEY_FILE"
    echo "[tls] сертификат создан в $CERT_DIR — при первом заходе по HTTPS браузер"
    echo "[tls] покажет предупреждение о самоподписанном сертификате, это ожидаемо."
else
    echo "[tls] использую существующий сертификат из $CERT_DIR"
fi

exec "$@"
