# Проект: FaceWatch — локальная система видеонаблюдения с AI-распознаванием лиц

## Обзор
- **Что это**: production-grade NVR с детекцией лиц, апскейлом и поиском похожих
- **Стек**: Python 3.11 + FastAPI, React 18 + TypeScript + Vite, PostgreSQL 16 + pgvector, Redis 7, MediaMTX
- **Архитектура**: Docker-сервисы (backend, worker, upscaler, frontend/nginx, mediamtx, postgres, redis, backup), два независимых слоя — запись и аналитика
- **Масштаб**: 12–250+ IP-камер (RTSP), CPU-only оптимизация (2× Xeon E5-2670)
- **Развёртывание**: .deb-пакет (`packaging/`, production) или Docker Compose (dev, Windows — `start.bat`/`stop.bat`)
- **Подробнее**: @README.md, @SPEC.md (ТЗ; разделы цитируются как `§N`)

## Ветки
- `main` в GitHub пустой (один файл `1`), рабочая ветка вливается в него PR #1.
  **Рабочая база — `claude/pensive-brown-28588t`**: от неё ответвляться, в неё открывать PR, с ней сравнивать при ревью.

## 🎯 Текущая цель — довести проект до рабочего MVP

Пока цель не снята пользователем, она **важнее порядка приоритетов SPEC §22**: не расширять
функциональность, а сделать так, чтобы то, что уже есть, надёжно работало у оператора.

### Что считается MVP
Система ставится одной командой (Docker Compose на Windows/Linux; .deb — следующий шаг) и 24/7 делает:
1. **Запись** всех включённых камер remux'ом без пропусков; архив воспроизводится и экспортируется
   фрагментом нужной длины (§2, §5)
2. **Live-просмотр** — мозаика и одна камера, H.264 и H.265, без чёрных плиток (§4)
3. **Распознавание** на камерах analytics: событие доходит до «Стены распознавания», персона
   создаётся/узнаётся, работает поиск по фото (§15)
4. **Камеры и пользователи**: добавление/редактирование/удаление камер через UI, вход, три роли (§3, §10, §18)
5. **Базовая надёжность**: переподключение RTSP, корректная остановка сервисов, ежедневный бэкап (§11, §13)
6. **CI зелёный** на рабочей ветке

Всё остальное (отчёты по расписанию, ONVIF Profile G, NUMA, калькуляторы, Grafana, тонкая настройка
профилей и т.п.) — **после MVP**, если только не мешает пунктам выше.

### Правила работы в режиме MVP
- **Выбирать задачу по порядку**: красный CI → дефект, ломающий пункт MVP → влить готовые PR → остальное.
  Новые функции не начинать, даже если они есть в ТЗ.
- **Исправление важнее отчёта.** Не писать новые аудиты, ревью-циклы и `docs/DEPLOY_*`-отчёты вместо
  починки. Документацию обновлять только в объёме сделанного изменения.
- **Один дефект — один небольшой PR** с тестом, который падал до исправления. Без попутного рефакторинга.
- **Не плодить ветки**: прежде чем начинать, проверить открытые PR (`gh api repos/IMBANEKONTRITSYA/Claude-NVR/pulls`) —
  возможно, исправление уже есть и его нужно довести и влить, а не писать заново.
- **Доводить до конца**: задача закрыта, когда CI зелёный и PR готов к мержу, а не когда написан код.
- Если для пункта MVP нужно решение пользователя (ограничение камер, железо, сеть) — остановиться и спросить,
  а не обходить молча.
- Сомневаешься, входит ли задача в MVP, — спроси одним вопросом до начала работы.

### Известные блокеры MVP
Источник истины — `docs/DEPLOY_FINDINGS_2026-10-28.md` и `docs/DEPLOY_RECHECK_2026-10-30.md`
(статус на 30.10.2026; перед работой сверить с кодом и открытыми PR):
- **CI красный на рабочей ветке**:
  - `test_mediamtx_version_matches_compose` (backend) — исправление в PR #162, не влито
  - upscaler: зависимости не ставятся в CI — исправление в PR #160, не влито
  - dependency-audit: `python-jose` / PYSEC-2026-4298 — исправления нет
  - сборка `deb` падает — причина не установлена
- **D04** — H.265: чёрные плитки live и нет снимка (PR #169, исправлено частично)
- **D05** — архив приписывает файлу отсутствующее видео, экспорт короткий (PR #163, не влит)
- **D06** — аналитика не держит ≥ 5 FPS на части камер — исправления нет
- **D09** — принудительное завершение части сервисов при остановке (PR #164, исправлено частично)
- **D10** — JWT попадает в лог backend через URL WebSocket (PR #165, не влит)
- **D12** — устаревший FPS у выключенных камер; **D13** — преждевременное «модель загружена» — исправлений нет
- **Открыто 15 PR (#160–#173)**, ни один не влит. Начинать с них: довести каждый до зелёного CI и подготовить к мержу
  (мержит пользователь). Отчётный PR #173 — не блокер MVP.
- **`main` пустой**: вся работа — в PR #1 (`claude/pensive-brown-28588t` → `main`). Влить его — решение пользователя,
  после зелёного CI.

## Команды

### Бэкенд
```bash
cd backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest pytest-cov aiosqlite aiosmtpd
.venv/bin/pytest -q                                   # как в CI
.venv/bin/pytest --cov=app --cov-fail-under=77 -q     # порог покрытия CI — 77%
```
Интеграционные тесты (`tests/test_integration_*.py`) требуют настоящих Postgres (pgvector:pg16) и Redis
через `DATABASE_URL`/`REDIS_URL`; без них они уходят в `skip` — зелёный прогон со skip не значит «проверено».
Для экспорта архива нужен `ffmpeg`, для бэкапа — `pg_dump`, иначе соответствующие тесты тоже пропускаются.

### Worker / Upscaler
```bash
cd worker   && pytest tests/ -q        # живые тесты MediaMTX: pytest tests/test_*_live.py -q
cd upscaler && pytest tests/ -q
```

### Фронтенд
```bash
cd frontend
npm ci --legacy-peer-deps
npm test           # vitest run (юнит-тесты *.test.ts рядом с модулями в src/)
npm run build      # tsc -b && vite build — это же проверка типов
```
Линтера (`npm run lint`) в проекте нет.

### E2E (pytest + Playwright, настоящий nginx)
```bash
./e2e/run-stack.sh                                   # поднять стенд (см. e2e/README.md)
E2E_BASE_URL=http://127.0.0.1:8080 pytest e2e -q
./e2e/run-stack.sh --stop
```

### Docker
```bash
docker compose up -d --build
docker compose logs -f backend    # FastAPI
docker compose logs -f worker     # детекция лиц, слой записи, embed-API :9000
docker compose logs -f upscaler   # GFPGAN
docker compose logs -f mediamtx   # RTSP/HLS
```

### Производительность
Бенчмарки — `perf/bench_*.py`, нагрузочные тесты — `loadtest/`.

## Архитектура

### Два независимых слоя (SPEC §2)

**Слой записи** (все камеры, не зависит от аналитики):
```
IP-камеры --RTSP(pull)--> MediaMTX --remux--> /media/segments (без декодирования)
                                 └--HLS--> live-просмотр
```

**Слой аналитики** (только камеры в режиме analytics):
```
IP-камеры --RTSP--> Worker (OpenCV + InsightFace)
                         ├--> PostgreSQL+pgvector (персоны, события)
                         ├--> Redis pub/sub (faces:new, faces:enhanced)
                         └--> /media/snapshots
                                 ↓
                    Upscaler (GFPGAN) <--upscale:queue
```

### Где что лежит
```
backend/app/
  main.py          # lifespan, apply_schema_migrations() — миграции схемы
  models.py        # все SQLAlchemy-модели (один файл)
  schemas.py       # Pydantic-схемы
  routers/         # эндпоинты: cameras, persons, archive, search, reports, system, ws, ...
  services/        # бизнес-логика: encryption (Fernet), pubsub, backup, export, ...
  auth.py, audit.py, config.py, profiles.py
backend/init.sql   # начальная схема БД
backend/tests/     # pytest; статические, юнит- и интеграционные тесты
worker/            # worker.py, record_layer.py, embed_api.py, model_loader.py, onvif_*.py, ...
upscaler/          # upscaler.py (GFPGAN, OpenCV-fallback)
frontend/src/      # App.tsx, api.ts, pages/*.tsx, логика интерфейса в src/*.ts + *.test.ts
mediamtx/          # конфигурация RTSP/HLS-сервера
backup/            # run.sh — pg_dump по расписанию
packaging/         # сборка .deb (build-deb.sh)
e2e/ perf/ loadtest/ scripts/ docs/
```

### Модель данных
- **persons** — карточки персон (известные + кластеризованные неизвестные)
- **face_events** — события распознавания с эмбеддингами 512D
- **cameras** — настройки камер (RTSP-учётки зашифрованы Fernet)
- **video_segments** — записи архива (remux без декодирования)
- **pgvector HNSW-индексы** для поиска по эмбеддингам

### Миграции схемы
Alembic **не используется**. Схема создаётся `Base.metadata.create_all` + `backend/init.sql`,
изменения существующих БД — идемпотентными операторами (`ADD COLUMN IF NOT EXISTS` и т.п.)
в `apply_schema_migrations()` в `backend/app/main.py`. Блок выполняется **одной транзакцией**:
оператор, падение которого допустимо, оборачивать в `conn.begin_nested()` (SAVEPOINT), иначе
упавший оператор откатит всё. Тест — `backend/tests/test_startup_migrations.py`.

## Стиль кода

### Python (backend, worker, upscaler)
- Python 3.11+, type hints обязательны
- FastAPI + Pydantic, SQLAlchemy 2.0 async
- Ошибки — `try/except` с логированием (`logger.error(..., exc_info=True)`), без проглатывания
- Комментарии и docstring'и на русском; комментарий объясняет **почему**, со ссылкой на `§N` ТЗ
- Автоформатера (black/ruff) в проекте нет — не переформатировать чужой код целыми файлами

```python
@router.get("/{person_id}")
async def get_person(person_id: int, db: AsyncSession = Depends(get_db)) -> PersonOut:
    """Получить карточку персоны по ID"""
    person = await db.get(Person, person_id)
    if not person:
        raise HTTPException(status_code=404, detail="Персона не найдена")
    return PersonOut.model_validate(person)
```

### TypeScript (frontend)
- React 18 + TypeScript strict, функциональные компоненты с hooks, именованные экспорты
- Страницы — `src/pages/`, чистая логика — отдельными модулями `src/*.ts` с тестом `*.test.ts` рядом
- Запросы к API — через `src/api.ts`
- Комментарии на русском

## Коммиты
Conventional Commits со ссылкой на раздел ТЗ, описание по-русски:
`fix(§5): прогноз места занижал расход ≈48×`, `feat(§4): ...`, `test(§2): ...`, `docs(review): ...`.

## Workflow
0. Проверить, что задача входит в MVP (раздел выше) и что её не решает уже открытый PR
1. Plan mode (`Shift+Tab`) — изучить нужный раздел SPEC.md и существующий код
2. Реализовать с минимальными изменениями
3. Тесты: pytest для backend/worker/upscaler, vitest для frontend, e2e для поведения в браузере
4. Проверить в Docker
5. Коммит в формате выше, PR в `claude/pensive-brown-28588t`

## ВАЖНО / Никогда

### ⛔ Никогда
- Не редактировать `.env` — только `.env.example`
- Не коммитить секреты (`SECRET_KEY`, `RTSP_ENCRYPTION_KEY`, `ADMIN_PASSWORD`)
- Не декодировать видео в слое записи — только remux (SPEC §2, §19)
- Не менять схему БД без миграции в `apply_schema_migrations()` и теста на неё
- Не трогать конфигурацию MediaMTX без понимания Control API
- Не писать тест «под то, что работает»: проверять контракт ТЗ (см. `e2e/README.md`)

### ✅ Всегда
- Шифровать RTSP-учётки через Fernet (`services/encryption.py`) перед сохранением
- Использовать pgvector для поиска по эмбеддингам
- Публиковать события в Redis pub/sub (`faces:new`, `faces:enhanced`)
- Проверять на всех трёх профилях производительности
- Добавлять тесты для новых эндпоинтов
- Обновлять документацию (ниже)

## Профили производительности (SPEC §15, таблица в README; требования — §19)
- **Экономный**: 5 FPS, каждый 4-й кадр, префильтр движения, buffalo_s, 640 px
- **Стандартный**: 10 FPS, префильтр, buffalo_s, аватары в фоне
- **Максимальный**: 15 FPS, без префильтра, buffalo_l, полный апскейл

Адаптивная частота: после 20 с без движения — `idle_fps`.

## Безопасность
- Пароли: bcrypt; JWT access-токен — `ACCESS_TOKEN_EXPIRE_MINUTES` (30 мин) + refresh-токен (`REFRESH_TOKEN_EXPIRE_DAYS`, 14 дн.)
- RTSP-учётки: Fernet; rate-limiting логина; журнал аудита
- Медиа и HLS — только по валидному токену (`auth_request` в nginx)
- Production: HTTPS через nginx, смена всех секретов

## Документация — обновлять при изменениях
- `docs/ARCHITECTURE.md` — архитектура
- `docs/API_DOCS.md` — эндпоинты
- `docs/USER_GUIDE.md`, `docs/ADMIN_GUIDE.md` — интерфейс и администрирование
- `docs/CHANGELOG.md` — история версий

## Мониторинг
- Метрики: `GET /api/system/metrics`; Prometheus: `GET /api/system/prometheus` (только админ)
- Слой записи: `GET /api/system/record-layer`; диск: `GET /api/system/storage`
- Очередь апскейла: `redis-cli LLEN upscale:queue`

## Резервное копирование
- Автоматически раз в сутки; хранение — `BACKUP_RETENTION_DAYS` (по умолчанию 14)
- Ручной запуск: `docker compose exec backup /backup/run.sh` (или из раздела «Настройки»)
- Восстановление: `docs/BACKUP.md`
