# FaceWatch

Локальная система видеонаблюдения с распознаванием лиц для IP-камер по RTSP.
Развёртывание на Windows 10/11 Pro одной командой через Docker Desktop.

## Возможности

- Подключение до 16 IP-камер по RTSP (добавление через веб-интерфейс)
- Просмотр в реальном времени (мозаика + полноэкранный режим)
- Детекция движения и лиц (InsightFace), извлечение эмбеддингов 512D
- Автоматическая кластеризация неизвестных персон в карточки (pgvector)
- Видеоархив с записью только при событиях, автоудаление через N дней
- Стена распознавания (live feed через WebSocket)
- Дашборд: KPI, графики по дням/часам, топ персон
- Зоны детекции (ROI) — рисование полигонов поверх кадра
- Отчёты Excel/CSV
- Роли: администратор, оператор, наблюдатель (bcrypt, JWT)
- RTSP-учётки шифруются в БД (Fernet/AES)
- Русский интерфейс, тёмная тема

## Стек

- **Бэкенд:** Python 3.11 + FastAPI + SQLAlchemy + WebSocket
- **Воркер:** OpenCV + InsightFace (buffalo_s, CPU) + scikit-learn
- **БД:** PostgreSQL 16 + pgvector
- **Кэш/PubSub:** Redis 7
- **Медиасервер:** MediaMTX (RTSP/HLS/WebRTC)
- **Фронтенд:** React 18 + TypeScript + Vite + Recharts
- **Веб-сервер:** Nginx (alpine)
- **Оркестрация:** Docker Compose

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
| Управление камерами   | ✅    | —        | —            |
| Настройка ROI         | ✅    | ✅       | —            |
| Архив, Карточки       | ✅    | ✅       | —            |
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
[ IP-камеры ] --RTSP--> [ Worker (OpenCV + InsightFace) ]
                                  │
                                  ├──> PostgreSQL+pgvector (персоны, события, сегменты)
                                  ├──> Redis Pub/Sub (faces:new, cameras:status)
                                  └──> /media (snapshots, segments)

[ Браузер ] <--HTTP/WS--> [ Nginx ] --> [ FastAPI backend ]
                                              │
                                              └──> Postgres / Redis / Media
```

## Ограничения текущей сборки (MVP)

- В мозаике камер показан статус, без встроенного HLS-плеера (для интеграции достаточно подключить `hls.js` и сконфигурировать публикацию потоков в MediaMTX через ffmpeg — задача расширения).
- Для ROI используется заглушка стоп-кадра — реальный кадр можно получить через эндпоинт-снапшот с воркера.
- Кластеризация выполняется онлайн через ближайший центроид; периодическая DBSCAN-переоценка может быть добавлена как фоновое задание.

## Безопасность

- Пароли пользователей: bcrypt (passlib).
- JWT с истечением через 12 часов.
- RTSP-строки шифруются Fernet перед записью в БД.
- Доступ к медиа и архивам — только по валидному токену.
- В production: смените `SECRET_KEY`, `RTSP_ENCRYPTION_KEY`, `ADMIN_PASSWORD`, поставьте Nginx за HTTPS.
