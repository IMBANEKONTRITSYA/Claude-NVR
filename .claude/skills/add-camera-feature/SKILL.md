---
name: add-camera-feature
description: Добавить новую функциональность для работы с камерами (SPEC §3–6, ONVIF, ROI, режимы). Используй при расширении возможностей видеонаблюдения.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Добавление функциональности для камер

## Шаги

1. **Изучи требования и существующий код**
   - Нужный раздел `SPEC.md` (§3 камеры, §4 live, §5 архив, §6 движение/ROI, §12 ONVIF)
   - Модель `Camera` — `backend/app/models.py`; схемы — `backend/app/schemas.py`
   - Эндпоинты — `backend/app/routers/cameras.py` (ONVIF — `routers/onvif.py`)
   - Сервисы — `backend/app/services/camera_config.py`, `encryption.py`
   - Worker — `worker/worker.py` (аналитика), `worker/record_layer.py` (пути MediaMTX через Control API `/v3/config/paths/*`)
   - UI — `frontend/src/pages/Cameras.tsx`, `ROI.tsx`, `LiveGrid.tsx`

2. **Модель данных (если нужно)**
   - Добавь поле в `backend/app/models.py`
   - Для существующих БД — идемпотентный оператор (`ADD COLUMN IF NOT EXISTS ...`) в `apply_schema_migrations()` в `backend/app/main.py`. Alembic в проекте нет.
   - Обнови Pydantic-схемы в `backend/app/schemas.py`

3. **Backend-эндпоинт**
   - Роутер в `backend/app/routers/`, `Depends(get_db)`, проверка роли по матрице прав SPEC §18
   - RTSP-учётки — только через Fernet (`services/encryption.py`)
   - После изменения камеры — публикация в Redis-канал `cameras:changed`: worker по нему перечитывает конфигурацию без перезапуска (SPEC §2)

4. **Worker (если нужно)**
   - Слой записи не декодирует видео — только remux; запись не должна зависеть от аналитики
   - Изменения путей MediaMTX — только через Control API в `record_layer.py`

5. **Frontend**
   - Страница в `frontend/src/pages/`, запросы через `src/api.ts`
   - Чистую логику — в отдельный модуль `src/<имя>.ts` с тестом `src/<имя>.test.ts`
   - Обработай loading и error

6. **Тесты и документация**
   - Backend: `backend/tests/` (интеграционные — `test_integration_*.py`, нужен Postgres)
   - Worker: `worker/tests/`; Frontend: vitest (`npm test`)
   - Действие оператора в браузере — `e2e/test_ui_actions.py`
   - Обнови `docs/API_DOCS.md`, `docs/USER_GUIDE.md`, `docs/CHANGELOG.md`

## Пример эндпоинта

```python
@router.put("/{camera_id}/roi")
async def update_roi(
    camera_id: int,
    roi: ROIUpdate,
    db: AsyncSession = Depends(get_db),
    user=Depends(require_role("admin", "operator")),  # SPEC §18: ROI — админ и оператор
):
    """Обновить зону детекции (ROI) камеры, SPEC §6"""
    camera = await db.get(Camera, camera_id)
    if not camera:
        raise HTTPException(status_code=404, detail="Камера не найдена")
    camera.roi_polygon = roi.polygon
    await db.commit()
    # Worker перечитает настройки камеры без перезапуска слоёв (SPEC §2)
    await publish("cameras:changed", {"camera_id": camera_id})
    return {"status": "ok"}
```

Имена `require_role`/`publish` сверь с `backend/app/auth.py` и `backend/app/services/pubsub.py` — пример иллюстративный.
