# Production-упаковка: .deb + systemd (SPEC §26, режим 2)

У FaceWatch ровно два поддерживаемых способа развёртывания (SPEC §26):

| Режим | Для чего | Чем поднимается |
|---|---|---|
| 1. Docker Compose | разработка, тестирование, CI, демо | `docker compose up -d` / `start.bat` |
| 2. **.deb + systemd** | **эксплуатация 24/7 на объекте** | `apt install ./facewatch_*.deb` |

Этот каталог — режим 2. Docker на объекте не нужен вовсе.

## Установка на объекте

```bash
sudo apt install ./facewatch_1.0.0+20260822.05792037_amd64.deb
```

Всё остальное пакет делает сам: ставит PostgreSQL с pgvector, Redis, nginx
и ffmpeg из репозитория дистрибутива, заводит системную учётку `facewatch`,
генерирует секреты и самоподписанный TLS-сертификат, создаёт роль и базу,
включает и запускает сервисы.

После установки:

```
интерфейс  https://<адрес сервера>/     (браузер предупредит о самоподписанном сертификате)
логин      admin
пароль     /etc/facewatch/ADMIN_PASSWORD   ← смените в интерфейсе и удалите файл
настройки  /etc/facewatch/facewatch.env
данные     /var/lib/facewatch/
логи       journalctl -u facewatch-backend -f
```

Апгрейд — `apt install ./facewatch_<новая версия>_amd64.deb`. Секреты,
конфигурация, архив и база остаются на месте; миграции схемы бэкенд
применяет сам при старте (идемпотентно).

## Что внутри пакета

```
/opt/facewatch/
  runtime/           переносимый CPython 3.11 (python-build-standalone)
  venv/{backend,worker,upscaler}/   по venv на сервис, поверх этого runtime
  app/{backend,worker,upscaler}/    код сервисов
  web/               собранный фронтенд, его отдаёт nginx
  bin/mediamtx       статический бинарник слоя записи
  models/            вложенная модель распознавания (buffalo_s)
/etc/facewatch/
  facewatch.env      общая конфигурация и секреты (создаётся при установке)
  backend.env        асинхронная форма DATABASE_URL для asyncpg
  mediamtx.env       пароль Control API MediaMTX
  mediamtx.yml       конфигурация слоя записи (conffile)
  tls/               самоподписанный сертификат
  ADMIN_PASSWORD     пароль первого входа
/var/lib/facewatch/
  media/segments/    архив; под него обычно монтируют отдельный диск
  media/{snapshots,avatars,uploads,thumbs}/
  backups/
  .insightface/      веса модели (переживают апгрейд пакета)
/lib/systemd/system/facewatch-*.service, facewatch.target
/usr/sbin/facewatch-first-run
```

Системного `python3` пакет не касается: сервисы запускаются
интерпретатором из `/opt/facewatch/runtime` (SPEC §26 — «самодостаточный
Python-рантайм ... без зависимости от системного python»).

## Сервисы

```bash
systemctl status facewatch.target      # все четыре разом
systemctl restart facewatch-worker     # или по одному
journalctl -u facewatch-worker -f
```

| Юнит | Слой (SPEC §2) | Лимиты (SPEC §26) |
|---|---|---|
| `facewatch-mediamtx` | запись | CPUQuota 400 %, MemoryMax 2G |
| `facewatch-worker` | аналитика и обслуживание архива | CPUQuota 600 %, MemoryMax 4G |
| `facewatch-backend` | приложение | CPUQuota 200 %, MemoryMax 1G |
| `facewatch-upscaler` | апскейл лиц | CPUQuota 200 %, MemoryMax 3G |
| `facewatch-first-run` | подготовка (oneshot) | — |

Числа взяты из `deploy.resources.limits` соответствующих сервисов
`docker-compose.yml`, чтобы объект и стенд вели себя одинаково.

У всех сервисов `Restart=always` (SPEC §13). `Requires` между слоями нет
намеренно: SPEC §2 требует, чтобы отказ аналитики не влиял на запись, и
наоборот — systemd-зависимость между ними это требование нарушала бы.

## Сборка пакета

```bash
sudo ./packaging/build-deb.sh --profile full
```

Нужен root: venv помнит абсолютный путь до интерпретатора, поэтому дерево
собирается сразу по боевому пути `/opt/facewatch`, а не в staging (см.
шапку `build-deb.sh`). Опции — `--profile core|full`, `--out DIR`,
`--prefix DIR`, `--frontend-dist DIR`.

Профили:

* `full` — всё, включая torch/GFPGAN для апскейла (§15);
* `core` — без torch/GFPGAN. `upscaler/upscaler.py` импортирует GFPGAN
  лениво, поэтому сервис стартует и работает очередью, а сам апскейл
  отдаёт понятную ошибку. Профиль для install-теста в CI.

### Почему `dpkg-deb`, а не `nfpm`/`fpm`

SPEC §26 называет nfpm/fpm. nfpm — бинарник с GitHub, fpm — ruby-гем; оба
добавляют в сборку ещё одну сетевую зависимость и ещё один пин, за которым
надо следить. `dpkg-deb` входит в `dpkg` и производит ровно тот же
артефакт, а карта файлов, права и скрипты сопровождения лежат в
`packaging/deb/` явным деревом — читать его глазами проще, чем YAML с
описанием того же дерева.

### Почему ffmpeg из репозитория, а не статическая сборка

SPEC §26 перечисляет в составе пакета статическую сборку FFmpeg. По
умолчанию пакет вместо этого объявляет `Depends: ffmpeg` — он есть и в
Debian 12, и в Ubuntu 24.04, и приезжает тем же `apt install
./facewatch_*.deb`, то есть установка остаётся одной командой. Причина:
единственный практичный источник статических сборок отдаёт неизменяемые
ссылки только на датированные теги, а `latest` меняется ежедневно — пин по
sha256 на него невозможен, и в production-пакет поехал бы неизвестно какой
бинарник. Доверенный пакет дистрибутива в этом размене выигрывает.

Кому нужна именно статика (изолированный объект со старым дистрибутивом):

```bash
sudo FACEWATCH_FFMPEG_TARBALL=/path/ffmpeg-static.tar.xz \
     FACEWATCH_FFMPEG_SHA256=<sha256> \
     ./packaging/build-deb.sh --profile full
```

Тогда `ffmpeg` и `ffprobe` кладутся в `/opt/facewatch/bin` и берутся
оттуда.

### Пины

`packaging/versions.env` — версия и **sha256** переносимого CPython и
MediaMTX. Несовпадение суммы роняет сборку: скачать и запустить чужой
бинарник без сверки в production-пакете нельзя.

Версия MediaMTX в `versions.env` сверяется с `docker-compose.yml` и при
расхождении сборка падает — иначе на объект поехал бы сервер записи не той
версии, против которой гоняются тесты слоя записи в CI.

## Install-тест

```bash
sudo ./packaging/install-test.sh dist/facewatch_*.deb
```

**Ломает машину, на которой запущен** (ставит и удаляет пакеты, правит
`/etc/nginx`, роль и базу PostgreSQL) — только одноразовый раннер или ВМ.

Проверяет то, что требует SPEC §26, и ещё два пункта, без которых первые
четыре ничего не стоят:

1. пакет ставится `apt install`;
2. сервисы `active` — и не в CrashLoop (счётчик `NRestarts` за первую
   минуту, иначе `Restart=always` маскировал бы падение);
3. интерфейс и API отвечают через nginx, HTTPS поднят;
4. вход `admin` работает, и полученным токеном действительно открывается
   защищённый эндпоинт;
5. миграции применились — проверка в самой БД (таблицы, pgvector,
   заведённый администратор), а не по строчке в логе;
6. повторная установка идемпотентна: секреты не перегенерированы, прежний
   токен продолжает работать;
7. `purge` уносит конфигурацию, но **не** архив и **не** базу.

В CI — `.github/workflows/package.yml`: на тег `v*` полный профиль, на PR,
трогающий упаковку, — `core`.
