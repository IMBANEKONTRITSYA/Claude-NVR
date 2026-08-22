#!/usr/bin/env bash
# Сборка production-пакета FaceWatch (SPEC §26, режим 2: .deb + systemd).
#
# Почему dpkg-deb, а не nfpm/fpm (их называет SPEC §26)
# ------------------------------------------------------
# nfpm — бинарник с GitHub, fpm — ruby-гем; оба добавляют в сборку ещё одну
# сетевую зависимость и ещё один пин, который надо сторожить. dpkg-deb
# входит в `dpkg`, то есть присутствует на любой машине, где этот пакет
# вообще имеет смысл собирать, и производит ровно тот же артефакт. Пины,
# ради которых обычно берут nfpm (карта файлов, права, скрипты сопровождения),
# здесь и так лежат явным деревом в packaging/deb/ — читать его глазами
# проще, чем YAML-описание того же дерева.
#
# Где собирается
# --------------
# venv помнит абсолютный путь до интерпретатора (pyvenv.cfg + шебанги
# скриптов), поэтому дерево собирается СРАЗУ по боевому пути ($PREFIX,
# по умолчанию /opt/facewatch), а не в staging с последующим переносом:
# перенесённый venv ищет python по пути сборки и на объекте не стартует.
# Отсюда требование root и `--clean`, стирающий прежнее содержимое.
#
# Профили
# -------
#   full  — всё, включая torch/GFPGAN для апскейла (§15). ~3 ГБ дерева.
#   core  — без torch/GFPGAN: апскейл импортирует GFPGAN лениво
#           (upscaler/upscaler.py::_load_gfpgan), поэтому сервис стартует и
#           работает как очередь, а апскейл отдаёт понятную ошибку. Профиль
#           для install-теста в CI: он проверяет упаковку, юниты, миграции и
#           вход, а не веса моделей.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${PREFIX:-/opt/facewatch}"
PROFILE="full"
OUT_DIR="$REPO_ROOT/dist"
FRONTEND_DIST=""
KEEP_BUILD=0

usage() {
    cat <<'EOF'
Использование: build-deb.sh [опции]

  --profile full|core   состав рантайма (по умолчанию full)
  --out DIR             куда положить .deb (по умолчанию <repo>/dist)
  --prefix DIR          боевой префикс установки (по умолчанию /opt/facewatch);
                        сборка идёт по этому же пути, см. шапку файла
  --frontend-dist DIR   взять готовую сборку фронтенда вместо `npm run build`
  --keep-build          не удалять промежуточный каталог
  -h, --help            это сообщение
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --profile) PROFILE="$2"; shift 2 ;;
        --out) OUT_DIR="$2"; shift 2 ;;
        --prefix) PREFIX="$2"; shift 2 ;;
        --frontend-dist) FRONTEND_DIST="$2"; shift 2 ;;
        --keep-build) KEEP_BUILD=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "неизвестная опция: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$PROFILE" in full|core) ;; *) echo "профиль должен быть full или core" >&2; exit 2 ;; esac
[ "$(id -u)" = "0" ] || { echo "нужен root: дерево собирается по боевому пути $PREFIX" >&2; exit 1; }

# shellcheck source=packaging/versions.env
. "$REPO_ROOT/packaging/versions.env"

# Версия MediaMTX — из docker-compose.yml, а не из versions.env: слой записи
# тестируется в CI против бинарника ровно этой версии, и разъехавшись, пакет
# повёз бы на объект непроверенный сервер.
COMPOSE_MTX="$(grep -oP 'image:\s*bluenviron/mediamtx:\K\S+' "$REPO_ROOT/docker-compose.yml" | head -1)"
if [ "$COMPOSE_MTX" != "$MEDIAMTX_VERSION" ]; then
    echo "MediaMTX: docker-compose.yml=$COMPOSE_MTX, versions.env=$MEDIAMTX_VERSION." >&2
    echo "Обновите обе точки вместе с MEDIAMTX_SHA256 — иначе пакет и CI проверяют разные серверы." >&2
    exit 1
fi

VERSION_BASE="$(tr -d '[:space:]' < "$REPO_ROOT/VERSION")"
# Ревизия пакета — дата и короткий хеш: пакеты, собранные из разных коммитов
# одной версии, обязаны различаться для `apt upgrade`.
GIT_SHA="$(git -C "$REPO_ROOT" rev-parse --short=8 HEAD 2>/dev/null || echo nogit)"
BUILD_DATE="${SOURCE_DATE_EPOCH:+$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y%m%d)}"
BUILD_DATE="${BUILD_DATE:-$(date -u +%Y%m%d)}"
DEB_VERSION="${VERSION_BASE}+${BUILD_DATE}.${GIT_SHA}"

BUILD_DIR="$(mktemp -d /tmp/facewatch-deb.XXXXXX)"
STAGE="$BUILD_DIR/stage"
cleanup() { [ "$KEEP_BUILD" = "1" ] || rm -rf "$BUILD_DIR"; }
trap cleanup EXIT

log() { printf '\033[1;34m[deb]\033[0m %s\n' "$*"; }

fetch() {  # url sha256 dest
    local url="$1" want="$2" dest="$3" got
    log "скачиваю $(basename "$dest")"
    curl -fsSL --retry 3 --retry-delay 2 -o "$dest" "$url"
    got="$(sha256sum "$dest" | cut -d' ' -f1)"
    if [ "$got" != "$want" ]; then
        echo "sha256 не совпал для $url" >&2
        echo "  ожидалось: $want" >&2
        echo "  получено:  $got" >&2
        exit 1
    fi
}

log "версия пакета: $DEB_VERSION, профиль: $PROFILE, префикс: $PREFIX"

# ---------------------------------------------------------------- рантайм
log "готовлю дерево $PREFIX"
rm -rf "$PREFIX"
mkdir -p "$PREFIX"/{bin,app,web,models,venv}

fetch "https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_RELEASE}/cpython-${PBS_PYTHON}+${PBS_RELEASE}-x86_64-unknown-linux-gnu-install_only.tar.gz" \
      "$PBS_SHA256" "$BUILD_DIR/python.tar.gz"
tar xzf "$BUILD_DIR/python.tar.gz" -C "$BUILD_DIR"
mv "$BUILD_DIR/python" "$PREFIX/runtime"
PY="$PREFIX/runtime/bin/python3"
"$PY" -c 'import ssl, sqlite3, ctypes, lzma' || { echo "переносимый python неполон" >&2; exit 1; }

fetch "https://github.com/bluenviron/mediamtx/releases/download/v${MEDIAMTX_VERSION}/mediamtx_v${MEDIAMTX_VERSION}_linux_amd64.tar.gz" \
      "$MEDIAMTX_SHA256" "$BUILD_DIR/mediamtx.tar.gz"
tar xzf "$BUILD_DIR/mediamtx.tar.gz" -C "$BUILD_DIR" mediamtx
install -m 0755 "$BUILD_DIR/mediamtx" "$PREFIX/bin/mediamtx"

# Статический ffmpeg — необязательная вкладка. По умолчанию пакет зависит от
# ffmpeg из репозитория дистрибутива (он есть и в Debian 12, и в Ubuntu
# 24.04, и ставится тем же `apt install ./facewatch_*.deb`), потому что
# единственный доступный источник статических сборок отдаёт неизменяемые
# ссылки только на датированные теги, а `latest` меняется ежедневно — пин по
# sha256 на него невозможен, а без пина вкладывать в production-пакет
# чужой бинарник хуже, чем взять доверенный из репозитория.
# Кому нужна именно статика (изолированный сервер со старым дистрибутивом) —
# FACEWATCH_FFMPEG_TARBALL=<путь|url> и, при желании, FACEWATCH_FFMPEG_SHA256.
if [ -n "${FACEWATCH_FFMPEG_TARBALL:-}" ]; then
    log "вкладываю статический ffmpeg"
    if [ -f "$FACEWATCH_FFMPEG_TARBALL" ]; then
        cp "$FACEWATCH_FFMPEG_TARBALL" "$BUILD_DIR/ffmpeg.tar"
    else
        curl -fsSL -o "$BUILD_DIR/ffmpeg.tar" "$FACEWATCH_FFMPEG_TARBALL"
    fi
    if [ -n "${FACEWATCH_FFMPEG_SHA256:-}" ]; then
        got="$(sha256sum "$BUILD_DIR/ffmpeg.tar" | cut -d' ' -f1)"
        [ "$got" = "$FACEWATCH_FFMPEG_SHA256" ] || { echo "sha256 ffmpeg не совпал" >&2; exit 1; }
    fi
    mkdir -p "$BUILD_DIR/ffmpeg"
    tar xf "$BUILD_DIR/ffmpeg.tar" -C "$BUILD_DIR/ffmpeg" --strip-components=1
    find "$BUILD_DIR/ffmpeg" -type f \( -name ffmpeg -o -name ffprobe \) -exec install -m 0755 {} "$PREFIX/bin/" \;
    [ -x "$PREFIX/bin/ffmpeg" ] || { echo "в архиве не нашлось ffmpeg" >&2; exit 1; }
fi

# ------------------------------------------------------------------- venv
mkvenv() {  # имя requirements-файл [доп-пакеты...]
    local name="$1" req="$2"; shift 2
    log "venv $name"
    "$PY" -m venv "$PREFIX/venv/$name"
    "$PREFIX/venv/$name/bin/pip" install --no-cache-dir --quiet --upgrade pip wheel
    "$PREFIX/venv/$name/bin/pip" install --no-cache-dir --quiet -r "$req" "$@"
}

mkvenv backend "$REPO_ROOT/backend/requirements.txt"
mkvenv worker "$REPO_ROOT/worker/requirements.txt"

if [ "$PROFILE" = "full" ]; then
    log "venv upscaler (с torch, профиль full)"
    "$PY" -m venv "$PREFIX/venv/upscaler"
    "$PREFIX/venv/upscaler/bin/pip" install --no-cache-dir --quiet --upgrade pip wheel
    "$PREFIX/venv/upscaler/bin/pip" install --no-cache-dir --quiet \
        torch==2.0.1 torchvision==0.15.2 --extra-index-url https://download.pytorch.org/whl/cpu
    "$PREFIX/venv/upscaler/bin/pip" install --no-cache-dir --quiet -r "$REPO_ROOT/upscaler/requirements.txt"
    # Та же несовместимость basicsr с новыми torchvision, что чинится в
    # upscaler/Dockerfile. На 0.15.2 замена безопасна.
    F="$("$PREFIX/venv/upscaler/bin/python" -c 'import basicsr,os;print(os.path.dirname(basicsr.__file__))')/data/degradations.py"
    sed -i 's/from torchvision.transforms.functional_tensor import/from torchvision.transforms.functional import/' "$F" || true
else
    log "venv upscaler (без torch, профиль core)"
    "$PY" -m venv "$PREFIX/venv/upscaler"
    "$PREFIX/venv/upscaler/bin/pip" install --no-cache-dir --quiet --upgrade pip wheel
    "$PREFIX/venv/upscaler/bin/pip" install --no-cache-dir --quiet \
        numpy==1.26.4 opencv-python-headless==4.10.0.84 redis==5.0.8 \
        sqlalchemy==2.0.34 psycopg2-binary==2.9.9 pgvector==0.3.4
fi

# ------------------------------------------------------------------- код
log "код сервисов"
mkdir -p "$PREFIX/app/backend" "$PREFIX/app/worker" "$PREFIX/app/upscaler"
cp -a "$REPO_ROOT/backend/app" "$PREFIX/app/backend/app"
# Глоб по всем .py в корне сервиса, а не поимённый список: именно на
# поимённом списке образы воркера и апскейла несколько раз падали в
# CrashLoop с ModuleNotFoundError, когда очередной модуль забывали дописать
# (см. шапку worker/Dockerfile и backend/tests/test_dockerfile_modules.py).
cp -a "$REPO_ROOT"/worker/*.py "$PREFIX/app/worker/"
cp -a "$REPO_ROOT"/upscaler/*.py "$PREFIX/app/upscaler/"
find "$PREFIX/app" -name '__pycache__' -type d -prune -exec rm -rf {} +

# ------------------------------------------------------------- фронтенд
if [ -n "$FRONTEND_DIST" ]; then
    log "фронтенд: беру готовую сборку из $FRONTEND_DIST"
    cp -a "$FRONTEND_DIST/." "$PREFIX/web/"
else
    log "фронтенд: npm ci && npm run build"
    command -v npm >/dev/null || { echo "нужен npm (или --frontend-dist DIR)" >&2; exit 1; }
    ( cd "$REPO_ROOT/frontend" && npm ci --legacy-peer-deps --silent && npm run build )
    cp -a "$REPO_ROOT/frontend/dist/." "$PREFIX/web/"
fi
[ -f "$PREFIX/web/index.html" ] || { echo "в сборке фронтенда нет index.html" >&2; exit 1; }

# --------------------------------------------------------------- модель
# Скачивание намеренно НЕ роняет сборку: пакет без модели остаётся рабочим
# (запись, ONVIF, архив), аналитика включится, когда модель положат руками —
# ровно та же логика, что в worker/Dockerfile.
log "модель распознавания $FACE_MODEL"
if HOME="$PREFIX/models" FACE_MODEL="$FACE_MODEL" \
   "$PREFIX/venv/worker/bin/python" - <<'PY'
import os
from insightface.app import FaceAnalysis
FaceAnalysis(name=os.environ["FACE_MODEL"], providers=["CPUExecutionProvider"])
PY
then
    log "модель вложена в пакет"
else
    log "МОДЕЛЬ НЕ ВЛОЖЕНА — положите её в /var/lib/facewatch/.insightface на объекте"
fi

# --------------------------------------------------------------- staging
log "собираю дерево пакета"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt" "$STAGE/etc/facewatch" \
         "$STAGE/etc/nginx/sites-available" "$STAGE/etc/nginx/snippets" \
         "$STAGE/lib/systemd/system" "$STAGE/usr/sbin" "$STAGE/usr/share/facewatch"
cp -a "$PREFIX" "$STAGE$PREFIX"

# Конфигурация MediaMTX собирается ИЗ ЖИВОГО mediamtx/mediamtx.yml: вторая
# копия конфигурации разошлась бы с первой на первой же правке, а слой
# записи разговаривает с сервером по этому файлу.
sed -e 's#^\( *recordPath:\).*#\1 /var/lib/facewatch/media/segments/%path_%s#' \
    "$REPO_ROOT/mediamtx/mediamtx.yml" > "$STAGE/etc/facewatch/mediamtx.yml"
grep -q '/var/lib/facewatch/media/segments' "$STAGE/etc/facewatch/mediamtx.yml" \
    || { echo "не удалось переписать recordPath в mediamtx.yml" >&2; exit 1; }

# nginx — тоже из живых файлов фронтенда, с заменой docker-имён на loopback.
sed -e 's#http://facewatch-backend:8000#http://127.0.0.1:8000#g' \
    -e 's#http://facewatch-mediamtx:8888#http://127.0.0.1:8888#g' \
    -e "s#^root /usr/share/nginx/html;#root $PREFIX/web;#" \
    "$REPO_ROOT/frontend/nginx-locations.conf" > "$STAGE/etc/nginx/snippets/facewatch-locations.conf"
grep -q 'facewatch-backend\|facewatch-mediamtx' "$STAGE/etc/nginx/snippets/facewatch-locations.conf" \
    && { echo "в locations остались docker-имена хостов" >&2; exit 1; }
cp "$REPO_ROOT/frontend/nginx-security-headers.conf" "$STAGE/etc/nginx/snippets/facewatch-security-headers.conf"
sed -e 's#/etc/nginx/snippets/nginx-locations.conf#/etc/nginx/snippets/facewatch-locations.conf#' \
    -e 's#/etc/nginx/snippets/nginx-security-headers.conf#/etc/nginx/snippets/facewatch-security-headers.conf#' \
    -i "$STAGE/etc/nginx/snippets/facewatch-locations.conf"
sed -e 's#/etc/nginx/snippets/nginx-locations.conf#/etc/nginx/snippets/facewatch-locations.conf#' \
    -e 's#/etc/nginx/certs/fullchain.pem#/etc/facewatch/tls/fullchain.pem#' \
    -e 's#/etc/nginx/certs/privkey.pem#/etc/facewatch/tls/privkey.pem#' \
    -e 's#access_log /dev/stdout redacted;#access_log /var/log/nginx/facewatch-access.log redacted;#' \
    "$REPO_ROOT/frontend/nginx.conf" > "$STAGE/etc/nginx/sites-available/facewatch.conf"

cp "$REPO_ROOT/packaging/deb/conf/facewatch.env.template" "$STAGE/usr/share/facewatch/facewatch.env.template"
install -m 0755 "$REPO_ROOT/packaging/deb/scripts/facewatch-first-run" "$STAGE/usr/sbin/facewatch-first-run"
install -m 0644 "$REPO_ROOT/packaging/deb/systemd/"*.service "$STAGE/lib/systemd/system/"
install -m 0644 "$REPO_ROOT/packaging/deb/systemd/facewatch.target" "$STAGE/lib/systemd/system/"

# Юниты и nginx-конфигурация знают префикс — он подставляется, а не
# зашивается: --prefix обязан работать целиком, иначе он ложь.
sed -i "s#@PREFIX@#$PREFIX#g" "$STAGE/lib/systemd/system/"*.service

INSTALLED_KB="$(du -sk "$STAGE" | cut -f1)"
sed -e "s#@VERSION@#$DEB_VERSION#" -e "s#@INSTALLED_SIZE@#$INSTALLED_KB#" \
    "$REPO_ROOT/packaging/deb/control" > "$STAGE/DEBIAN/control"
cp "$REPO_ROOT/packaging/deb/conffiles" "$STAGE/DEBIAN/conffiles"
for s in postinst prerm postrm; do
    sed -e "s#@PREFIX@#$PREFIX#g" "$REPO_ROOT/packaging/deb/$s" > "$STAGE/DEBIAN/$s"
    chmod 0755 "$STAGE/DEBIAN/$s"
done

# ----------------------------------------------------------------- сборка
mkdir -p "$OUT_DIR"
DEB="$OUT_DIR/facewatch_${DEB_VERSION}_amd64.deb"
log "dpkg-deb --build (это минуту-две: дерево ~$((INSTALLED_KB/1024)) МБ)"
# --root-owner-group: без него понадобился бы fakeroot, а владельцем файлов
# в архиве стал бы тот, кто собирал.
#
# zstd, а не xz: полезная нагрузка здесь — гигабайты venv'ов, и однопоточный
# `xz -6` жал их 20+ минут (замерено в песочнице на 1.9 ГБ дерева профиля
# core), то есть дольше, чем занимает вся остальная сборка. zstd на том же
# дереве укладывается в десятки секунд при разнице в размере в проценты, и
# поддерживается dpkg начиная с 1.21 — то есть и в Debian 12, и в Ubuntu
# 24.04, единственных production-целях SPEC §26.
dpkg-deb --root-owner-group -Zzstd -z12 --threads-max="$(nproc)" --build "$STAGE" "$DEB" >/dev/null

log "готово: $DEB ($(du -h "$DEB" | cut -f1), развёрнутым $((INSTALLED_KB/1024)) МБ)"
echo "$DEB"
