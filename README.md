# FaceWatch

Локальная система видеонаблюдения с распознаванием лиц для IP-камер по RTSP.
Развёртывание на Windows 10/11 Pro одной командой через Docker Desktop.

## Возможности

- Подключение до 16 IP-камер по RTSP (добавление через веб-интерфейс, проверка подключения)
- Просмотр в реальном времени (мозаика + полноэкранный режим), bounding box'ы лиц с именами
- Детекция движения и лиц (InsightFace), извлечение эмбеддингов 512D
- **Нейросетевой апскейл лиц** (GFPGAN) — асинхронная очередь, улучшает скриншоты и аватары
- **Поиск похожих лиц по фото** (reverse search через pgvector) с порогом и фильтрами
- Автоматическая кластеризация неизвестных персон в карточки (онлайн + фоновый DBSCAN)
- Видеоархив с записью только при событиях, автоудаление через N дней
- Стена распознавания (live feed через WebSocket, подмена на улучшенное фото)
- Дашборд: KPI, графики по дням/часам, топ персон, тепловая карта часов пик
- Зоны детекции (ROI) — рисование полигонов поверх реального кадра
- Отчёты Excel/CSV: появления, сводка по персонам, активность по камерам, результаты поиска
- Роли: администратор, оператор, наблюдатель (bcrypt, JWT), смена пароля
- RTSP-учётки шифруются в БД (Fernet/AES)
- Русский интерфейс, тёмная тема

## Стек

- **Бэкенд:** Python 3.11 + FastAPI + SQLAlchemy + WebSocket
- **Воркер:** OpenCV + InsightFace (buffalo_s, CPU) + scikit-learn + embed-API
- **Апскейл:** отдельный сервис на GFPGAN (CPU), OpenCV-fallback
- **БД:** PostgreSQL 16 + pgvector
- **Кэш/очереди/PubSub:** Redis 7
- **Медиасервер:** MediaMTX (RTSP/HLS/WebRTC)
- **Фронтенд:** React 18 + TypeScript + Vite + Recharts
- **Веб-сервер:** Nginx (alpine)
- **Оркестрация:** Docker Compose (7 сервисов)

## Запуск под Windows

### Что нужно установить

1. **Docker Desktop for Windows**
   https://www.docker.com/products/docker-desktop/
   После установки запустите Docker Desktop и дождитесь зелёного индикатора.

### Старт системы

1. Распакуйте проект в любую папку (например, `C:\FaceWatch`).
2. Дважды кликните на `start.bat`.
3. Скрипт:
   - проверит наличие Docker;
   - создаст `.env` из шаблона (если ещё нет);
   - выполнит `docker compose up -d --build` (первая сборка занимает ~10 минут — скачиваются образы и модели InsightFace);
   - откроет браузер на http://localhost:8080.
4. Логин: `admin`, пароль: значение `ADMIN_PASSWORD` из `.env` (по умолчанию `admin` — **смените после первого входа**).

### Остановка

Запустите `stop.bat` или выполните `docker compose down` в папке проекта.

### Логи

```
docker compose logs -f backend
docker compose logs -f worker
docker compose logs -f frontend
```

## Адреса

| Сервис        | URL                          |
|---------------|------------------------------|
| Веб-интерфейс | http://localhost:8080        |
| API + Swagger | http://localhost:8000/docs   |
| MediaMTX RTSP | rtsp://localhost:8554        |
| MediaMTX HLS  | http://localhost:8888        |
| PostgreSQL    | localhost:5432               |
| Redis         | localhost:6379               |

## Добавление камеры

1. Войдите как админ.
2. **Управление камерами → Добавить камеру**.
3. Укажите название, RTSP URL вида `rtsp://user:pass@192.168.1.10:554/Streaming/Channels/101`, локацию.
4. Сохраните. Воркер в течение 10 секунд подхватит камеру и начнёт обработку.

## Матрица прав

| Действие              | Админ | Оператор | Наблюдатель |
|-----------------------|:-----:|:--------:|:-----------:|
| Просмотр онлайн       | ✅    | ✅       | ✅           |
| Дашборд, Стена        | ✅    | ✅       | ✅           |
| Поиск по фото         | ✅    | ✅       | —            |
| Управление камерами   | ✅    | —        | —            |
| Настройка ROI         | ✅    | ✅       | —            |
| Архив, Карточки       | ✅    | ✅       | —            |
| Ручной апскейл лица   | ✅    | ✅       | —            |
| Экспорт отчётов       | ✅    | ✅       | —            |
| Управление пользователями | ✅ | —      | —            |

## Конфигурация (.env)

```
POSTGRES_USER=facewatch
POSTGRES_PASSWORD=facewatch
SECRET_KEY=please-change-me
RTSP_ENCRYPTION_KEY=<32-байтный Fernet-ключ base64>
ADMIN_PASSWORD=admin
RETENTION_DAYS=30
```

Сгенерировать новый ключ Fernet:
```
docker run --rm python:3.11-slim sh -c "pip install cryptography -q && python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'"
```

## Архитектура

```
[ IP-камеры ] --RTSP--> [ Worker (OpenCV + InsightFace) ] --ffmpeg--> [ MediaMTX ] --HLS-->
                                  │  embed-API :9000 (поиск по фото)
                                  ├──> PostgreSQL+pgvector (персоны, события, сегменты)
                                  ├──> Redis: pub/sub (faces:new, faces:enhanced) + очередь upscale:queue
                                  └──> /media (snapshots, segments)
                                          │
[ Upscaler (GFPGAN) ] <--upscale:queue---┘  улучшает снимок → faces:enhanced

[ Браузер ] <--HTTP/WS--> [ Nginx ] --> [ FastAPI backend ] --> Postgres / Redis / Media
                                  └--/hls/--> MediaMTX        └--/embed--> Worker (поиск по фото)
```

### Поток нейросетевого апскейла
1. Воркер сохраняет исходный скриншот лица и кладёт `{event_id}` в `upscale:queue`.
2. Сервис `upscaler` берёт задачу, прогоняет через GFPGAN (или OpenCV-fallback),
   сохраняет улучшенную версию, обновляет `snapshot_path`/аватар, публикует `faces:enhanced`.
3. Бэкенд транслирует событие в WebSocket → Стена и карточки персон подменяют фото на лету.
   Оригинал сохраняется (`orig_snapshot_path`) на случай артефактов.

### Поиск похожих лиц
Бэкенд принимает фото → отправляет в embed-API воркера (InsightFace) → получает эмбеддинг →
pgvector-поиск по `face_events` с сортировкой по косинусной схожести и фильтрами (порог, дата, статус).

## Поток в реальном времени

Воркер автоматически репабликует каждый активный RTSP в MediaMTX через `ffmpeg -c copy`:
- внутренний адрес: `rtsp://mediamtx:8554/cam{id}`
- HLS-плейлист: `http://localhost:8080/hls/cam{id}/index.m3u8` (проксируется nginx-ом)

Браузер играет поток через `hls.js`; задержка обычно 2–4 секунды. Если HLS не доступен, плитка
автоматически переключается на последний снимок камеры (обновляется воркером каждые 2 сек).

## Ограничения текущей сборки

- ROI-редактор использует последний снимок камеры; при сильном изменении сцены имеет смысл нажать
  «Сбросить текущий» и перерисовать полигон.
- Кластеризация работает в двух режимах одновременно: онлайн (ближайший центроид) + фоновый DBSCAN
  раз в час (объединяет дубликаты неизвестных за последние 7 дней).

## Разработка

```bash
# Бэкенд: тесты безопасности (шифрование RTSP, JWT, пароли)
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest
.venv/bin/pytest -q

# Фронтенд: сборка и проверка типов
cd frontend
npm ci --legacy-peer-deps
npm run build
```

CI (GitHub Actions, `.github/workflows/ci.yml`) на каждый push прогоняет тесты бэкенда
и сборку фронтенда.

## Безопасность

- Пароли пользователей: bcrypt (passlib).
- JWT с истечением через 12 часов.
- RTSP-строки шифруются Fernet перед записью в БД.
- Доступ к медиа и архивам — только по валидному токену.
- В production: смените `SECRET_KEY`, `RTSP_ENCRYPTION_KEY`, `ADMIN_PASSWORD`, поставьте Nginx за HTTPS.
