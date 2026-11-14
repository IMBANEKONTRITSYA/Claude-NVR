---
name: fix-face-detection
description: Исправить проблемы с детекцией и распознаванием лиц — InsightFace, эмбеддинги, кластеризация, апскейл, живая лента (SPEC §15).
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Исправление проблем с детекцией лиц

## Шаги

1. **Диагностика**
   - Логи: `docker compose logs -f worker` (и `upscaler`)
   - Загрузилась ли модель (buffalo_s/buffalo_l) — `worker/model_loader.py`; сбой модели не должен ронять запись (`worker/tests/test_model_failure_isolation.py`)
   - FPS детекции — раздел «Мониторинг» / `GET /api/system/metrics`
   - Очередь апскейла: `redis-cli LLEN upscale:queue`

2. **Конфигурация**
   - Режим камеры: `record_only` или `analytics` — детекция только во втором
   - Расписание детекции (`worker/detection_schedule.py`), ROI
   - Профиль производительности, префильтр движения, `idle_fps`

3. **Отладка детекции** (`worker/worker.py`, `analytics_source.py`, `face_select.py`)
   - Источник кадров ≥ 640×480
   - Bounding box'ы и выбор лучшего кадра лица
   - Эмбеддинги 512D, нормализованы; сравнение по косинусному расстоянию
   - Порог схожести (0.1–0.9) из настроек

4. **Кластеризация** (`worker/worker.py`: `recluster_unknowns`, `_recluster_bg`)
   - Онлайн: ближайший центроид
   - Фоновый DBSCAN; объединение дубликатов неизвестных
   - Ручное слияние персон пересчитывает центроид

5. **Доставка события**
   - Worker публикует `faces:new` в Redis → backend `routers/ws.py` → «Стена распознавания» (`frontend/src/pages/Wall.tsx`)
   - Upscaler публикует `faces:enhanced` → лента подменяет фото

6. **Тесты**
   - `cd worker && pytest tests/ -q` (например, `test_face_modules.py`, `test_face_select.py`)
   - Кластеризация на настоящих эмбеддингах, а не на синтетике
   - Живая лента в браузере — `e2e/test_ui_live.py`

## Частые проблемы

**Низкий FPS** — включи префильтр движения, buffalo_s вместо buffalo_l, кадр 640 px, проверь `ort_threads.py` и привязку к ядрам.

**Плохая кластеризация** — порог косинусной схожести, параметры DBSCAN (eps, min_samples), частота фоновой рекластеризации.

**Очередь апскейла растёт** — понизь приоритет/частоту апскейла, включи «ленивый» режим (только аватар), проверь OpenCV-fallback.
