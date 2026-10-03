# INSTALL (Linux, production) — .deb + systemd

Production-развёртывание FaceWatch на объекте. Это **режим 2** SPEC §26 —
единственный поддерживаемый способ эксплуатации 24/7. Docker на объекте не
нужен: он остаётся режимом разработки и тестирования
([INSTALL.md](INSTALL.md), Windows).

Технические подробности сборки пакета и его состав —
[`packaging/README.md`](../packaging/README.md).

## 1. Требования

* **Debian 12+ или Ubuntu 24.04+**, x86-64. Других production-целей у
  проекта нет (SPEC §26).
* Права root (`sudo`).
* Железо — по таблице SPEC §20. Ориентиры: 12–32 камеры — N100/Celeron,
  8–16 ГБ; 32–128 камер — Core i3-i5/Ryzen, 32–64 ГБ; 128–256+ —
  Xeon E5/EPYC или два процессора, 64–256 ГБ.
* Диск под архив: `битрейт(Mbps) × 10.8 = ГБ/сутки на камеру`
  (SPEC §16). Калькулятор есть в админ-панели после установки.
* Доступ в интернет **на время установки** — apt тянет PostgreSQL, Redis,
  nginx и ffmpeg. Сам FaceWatch, MediaMTX, Python-рантайм и модель
  распознавания уже лежат в пакете, так что после установки объект может
  жить изолированным.

## 2. Установка

```bash
sudo apt install ./facewatch_1.0.0+20260822.05792037_amd64.deb
```

Одна команда — как требует SPEC §26. Пакет сам:

* доставит PostgreSQL с pgvector, Redis, nginx и ffmpeg из репозитория;
* заведёт системную учётку `facewatch` без права входа;
* сгенерирует секреты (`SECRET_KEY`, ключ шифрования RTSP-учёток, пароль
  администратора, пароль Control API MediaMTX, пароль БД);
* создаст роль и базу PostgreSQL и включит расширение `vector`;
* выпустит самоподписанный TLS-сертификат;
* включит свой сайт в nginx (и **отключит** дефолтный сайт дистрибутива —
  он занимает 80-й порт);
* поднимет четыре сервиса и включит их автозапуск.

Схему БД бэкенд создаёт сам при первом старте, миграции идемпотентны.

## 3. Первый вход

```
адрес   https://<адрес сервера>/
логин   admin
пароль  sudo cat /etc/facewatch/ADMIN_PASSWORD
```

Браузер предупредит о самоподписанном сертификате — это ожидаемо (SPEC
§26: сертификат «заменяем на боевой», см. §7 ниже).

**Сразу после входа** смените пароль администратора в интерфейсе и
удалите файл:

```bash
sudo rm /etc/facewatch/ADMIN_PASSWORD
```

## 4. Проверка после установки

```bash
systemctl status facewatch.target       # все сервисы
systemctl status facewatch-mediamtx     # слой записи
journalctl -u facewatch-backend -f      # логи приложения
curl -sk https://127.0.0.1/api/health
```

Развёрнутый чек-лист первого запуска на объекте, включая то, что не
проверяется ничем, кроме живых камер, —
[`DEPLOY_CHECKLIST.md`](DEPLOY_CHECKLIST.md).

## 5. Где что лежит

| Что | Путь |
|---|---|
| Конфигурация и секреты | `/etc/facewatch/facewatch.env` |
| Конфигурация слоя записи | `/etc/facewatch/mediamtx.yml` |
| TLS-сертификат | `/etc/facewatch/tls/` |
| Архив, снимки, аватары | `/var/lib/facewatch/media/` |
| Бэкапы | `/var/lib/facewatch/backups/` |
| Код и рантайм | `/opt/facewatch/` |
| Логи | `journalctl -u facewatch-*` |

После правки `/etc/facewatch/facewatch.env`:

```bash
sudo systemctl restart facewatch.target
```

### Отдельный диск под архив

Обычная раскладка объекта: система на SSD, архив на RAID из HDD.
Примонтируйте массив в `/var/lib/facewatch/media` (тогда менять ничего не
нужно) либо укажите свой путь в `MEDIA_PATH` и одновременно в `recordPath`
`/etc/facewatch/mediamtx.yml` — их значения обязаны совпадать, иначе
MediaMTX будет писать в одно место, а архив читаться из другого.

Владелец каталога — `facewatch:facewatch`.

## 6. Обновление

```bash
sudo apt install ./facewatch_<новая версия>_amd64.deb
```

Секреты, конфигурация, архив, база и веса модели остаются на месте;
сервисы перезапускаются с новым кодом, миграции схемы применяются при
старте бэкенда. Понижение версии пакетом не поддерживается — миграции
идут только вперёд.

## 7. Боевой TLS-сертификат

Замените два файла и перезагрузите nginx:

```bash
sudo cp fullchain.pem /etc/facewatch/tls/fullchain.pem
sudo cp privkey.pem   /etc/facewatch/tls/privkey.pem
sudo chmod 0640 /etc/facewatch/tls/privkey.pem
sudo systemctl reload nginx
```

Апгрейд пакета их не трогает.

## 8. Удаление

```bash
sudo apt remove facewatch    # сервисы остановлены, код удалён
sudo apt purge  facewatch    # плюс /etc/facewatch
```

**Ни `remove`, ни `purge` не удаляют архив** (`/var/lib/facewatch`) **и не
удаляют базу PostgreSQL.** Записи инцидента бывают в единственном
экземпляре, и пакетный менеджер — не тот инструмент, которым их стирают.
Если данные действительно больше не нужны:

```bash
sudo rm -rf /var/lib/facewatch
sudo -u postgres dropdb facewatch
sudo -u postgres dropuser facewatch
```

Дефолтный сайт nginx, отключённый при установке, обратно не включается —
`postrm` печатает команду, если он вам нужен.

## 9. Устранение неполадок

**Сервис не поднимается.** `journalctl -u facewatch-backend -n 100`.
Самая частая причина при ручной правке — опечатка в
`/etc/facewatch/facewatch.env`.

**PostgreSQL не отвечал в момент установки.** Скрипт первого запуска
печатает предупреждение и не создаёт роль с базой. Поднимите СУБД и
повторите — он идемпотентен:

```bash
sudo systemctl start postgresql
sudo systemctl start facewatch-first-run
sudo systemctl restart facewatch.target
```

**nginx не стартует после установки.** Скорее всего, 80-й или 443-й порт
занят вашим сайтом. Наш конфиг — `/etc/nginx/sites-available/facewatch.conf`;
правьте `listen` в нём.

**Аналитика не работает, запись идёт.** Так и задумано (SPEC §2 — слои
независимы). Причина видна в «Мониторинге» и в
`journalctl -u facewatch-worker`; чаще всего это отсутствие весов модели
на изолированном сервере — положите их в `/var/lib/facewatch/.insightface`
и перезапустите `facewatch-worker`.

**Апскейл лиц отдаёт ошибку про `gfpgan`.** Пакет собран в профиле `core`
(без torch/GFPGAN). Для объекта нужен профиль `full` — см.
[`packaging/README.md`](../packaging/README.md).
