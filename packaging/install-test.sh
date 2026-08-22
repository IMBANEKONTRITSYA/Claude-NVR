#!/usr/bin/env bash
# Install-тест production-пакета (SPEC §26: «install-тест на чистом раннере
# с systemd: пакет ставится, сервисы active, логин работает, миграции
# применились»).
#
# Проверяет ровно эти четыре пункта плюс два, без которых они ничего не
# стоят: идемпотентность повторной установки и сохранность данных при
# purge — то и другое SPEC §26 требует прямым текстом.
#
# ЛОМАЕТ МАШИНУ, на которой запущен: ставит и удаляет пакеты, правит
# /etc/nginx, /etc/facewatch, роль и базу PostgreSQL. Запускать только на
# одноразовом раннере или в одноразовой ВМ.
set -euo pipefail

DEB="${1:?использование: install-test.sh <путь к .deb>}"
FAILED=0

ok()   { printf '\033[1;32m  ✓\033[0m %s\n' "$*"; }
bad()  { printf '\033[1;31m  ✗\033[0m %s\n' "$*"; FAILED=1; }
step() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

dump_journal() {
    echo "--- journalctl (последние 60 строк на сервис) ---"
    for u in facewatch-first-run facewatch-mediamtx facewatch-backend \
             facewatch-worker facewatch-upscaler; do
        echo "### $u"
        journalctl -u "$u" --no-pager -n 60 2>&1 | tail -60 || true
    done
}
trap 'if [ "$FAILED" != "0" ]; then dump_journal; fi' EXIT

# Ждать состояния, а не спать фиксированно: на медленном раннере сон
# оказался бы коротким, на быстром — потраченным впустую.
wait_for() {  # описание таймаут команда...
    local what="$1" timeout="$2"; shift 2
    local deadline=$(( SECONDS + timeout ))
    while [ "$SECONDS" -lt "$deadline" ]; do
        if "$@" >/dev/null 2>&1; then
            ok "$what (за $(( SECONDS - (deadline - timeout) )) с)"
            return 0
        fi
        sleep 2
    done
    bad "$what — не дождался за ${timeout} с"
    return 1
}

step "1. Установка: apt install ./$(basename "$DEB")"
T0=$SECONDS
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$DEB"
INSTALL_SEC=$(( SECONDS - T0 ))
ok "пакет установлен за ${INSTALL_SEC} с"
echo "  размер файла: $(du -h "$DEB" | cut -f1), развёрнутым: $(du -sh /opt/facewatch | cut -f1)"

step "2. Сервисы active (SPEC §26)"
for u in facewatch-mediamtx facewatch-backend facewatch-worker facewatch-upscaler; do
    wait_for "$u активен" 90 systemctl is-active --quiet "$u.service" || true
done
# first-run — oneshot с RemainAfterExit, у него состояние active (exited).
if systemctl is-active --quiet facewatch-first-run.service; then
    ok "facewatch-first-run отработал"
else
    bad "facewatch-first-run не в состоянии active"
fi
# Restart=always маскирует падение: сервис, который валится и поднимается по
# кругу, между попытками выглядит активным. Счётчик перезапусков за первую
# минуту жизни отличает «работает» от «крутится в CrashLoop».
sleep 20
for u in facewatch-mediamtx facewatch-backend facewatch-worker; do
    n="$(systemctl show -p NRestarts --value "$u.service")"
    if [ "${n:-0}" -le 1 ]; then
        ok "$u не перезапускался (NRestarts=$n)"
    else
        bad "$u перезапускался $n раз — похоже на CrashLoop"
    fi
done

step "3. Интерфейс и API через nginx"
wait_for "GET /api/health отвечает 200" 60 \
    curl -fsS -o /dev/null http://127.0.0.1/api/health || true
if curl -fsS http://127.0.0.1/ | grep -qi '<div id="root"'; then
    ok "nginx отдаёт собранный фронтенд"
else
    bad "по / не пришёл index.html фронтенда"
fi
# TLS: SPEC §26 требует самоподписанный сертификат из коробки.
if curl -fsSk -o /dev/null https://127.0.0.1/api/health; then
    ok "HTTPS работает (самоподписанный сертификат)"
else
    bad "HTTPS не отвечает"
fi

step "4. Логин администратора (SPEC §26)"
ADMIN_PW="$(sudo cat /etc/facewatch/ADMIN_PASSWORD)"
[ -n "$ADMIN_PW" ] || bad "пустой /etc/facewatch/ADMIN_PASSWORD"
# Форма, а не JSON: обработчик объявлен через OAuth2PasswordRequestForm
# (backend/app/routers/auth.py), то есть ждёт application/x-www-form-urlencoded.
# --data-urlencode обязателен — в сгенерированном пароле есть '!' и он
# может содержать любые символы алфавита.
LOGIN_JSON="$(curl -fsS -X POST http://127.0.0.1/api/auth/login \
    --data-urlencode "username=admin" \
    --data-urlencode "password=$ADMIN_PW" \
    || echo '')"
TOKEN="$(printf '%s' "$LOGIN_JSON" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("access_token",""))
except Exception: print("")')"
if [ -n "$TOKEN" ]; then
    ok "вход admin выполнен, access-токен получен"
else
    bad "вход admin не удался: $LOGIN_JSON"
fi
# Токен обязан работать на защищённом эндпоинте: «логин работает» — это не
# «сервер вернул строку», а «этой строкой можно пользоваться».
if [ -n "$TOKEN" ] && curl -fsS -o /dev/null -H "Authorization: Bearer $TOKEN" \
        http://127.0.0.1/api/cameras; then
    ok "с полученным токеном отвечает /api/cameras"
else
    bad "токен не принимается защищённым эндпоинтом"
fi

step "5. Миграции применились (SPEC §26)"
# Смотрим в саму БД, а не в лог: лог сказал бы, что бэкенд «применил
# миграции», даже если бы применял их в другую базу.
TABLES="$(sudo runuser -u postgres -- psql -d facewatch -tAc \
    "select string_agg(tablename, ',' order by tablename) from pg_tables where schemaname='public'")"
for t in users cameras faces persons events; do
    if printf '%s' "$TABLES" | grep -qw "$t"; then
        ok "таблица $t создана"
    else
        bad "таблицы $t нет (есть: ${TABLES:-<пусто>})"
    fi
done
if sudo runuser -u postgres -- psql -d facewatch -tAc \
        "select 1 from pg_extension where extname='vector'" | grep -q 1; then
    ok "расширение pgvector включено"
else
    bad "расширения pgvector нет — поиск по лицам не работал бы"
fi
if [ "$(sudo runuser -u postgres -- psql -d facewatch -tAc \
        "select count(*) from users where username='admin'")" = "1" ]; then
    ok "администратор заведён в БД"
else
    bad "в таблице users нет admin"
fi

step "6. Идемпотентность: повторная установка того же пакета (SPEC §26)"
SECRET_BEFORE="$(sudo md5sum /etc/facewatch/facewatch.env | cut -d' ' -f1)"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --reinstall "$DEB"
SECRET_AFTER="$(sudo md5sum /etc/facewatch/facewatch.env | cut -d' ' -f1)"
if [ "$SECRET_BEFORE" = "$SECRET_AFTER" ]; then
    ok "секреты не перегенерированы"
else
    bad "переустановка переписала /etc/facewatch/facewatch.env — на объекте это потеря доступа к камерам"
fi
wait_for "бэкенд снова отвечает после переустановки" 90 \
    curl -fsS -o /dev/null http://127.0.0.1/api/health || true
if [ -n "$TOKEN" ] && curl -fsS -o /dev/null -H "Authorization: Bearer $TOKEN" \
        http://127.0.0.1/api/cameras; then
    ok "прежний токен продолжает работать (SECRET_KEY пережил переустановку)"
else
    bad "после переустановки прежний токен отвергнут"
fi

step "7. Удаление и purge: данные не теряются (SPEC §26)"
echo "проверка" | sudo tee /var/lib/facewatch/media/segments/canary.txt >/dev/null
sudo DEBIAN_FRONTEND=noninteractive apt-get purge -y -qq facewatch
if [ -f /var/lib/facewatch/media/segments/canary.txt ]; then
    ok "архив пережил purge"
else
    bad "purge стёр /var/lib/facewatch — архив объекта нельзя удалять пакетным менеджером"
fi
if [ ! -d /etc/facewatch ]; then
    ok "purge убрал конфигурацию (политика Debian)"
else
    bad "purge оставил /etc/facewatch"
fi
if sudo runuser -u postgres -- psql -tAc \
        "select 1 from pg_database where datname='facewatch'" | grep -q 1; then
    ok "база PostgreSQL пережила purge"
else
    bad "purge удалил базу — там события, персоны и эмбеддинги"
fi
for u in facewatch-backend facewatch-worker facewatch-mediamtx; do
    if systemctl is-active --quiet "$u.service"; then
        bad "$u продолжает работать после purge"
    else
        ok "$u остановлен"
    fi
done

step "Итог"
if [ "$FAILED" = "0" ]; then
    printf '\033[1;32minstall-тест пройден\033[0m\n'
else
    printf '\033[1;31minstall-тест ПРОВАЛЕН\033[0m\n'
fi
exit "$FAILED"
