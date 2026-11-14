---
name: write-tests
description: Написать тесты для FaceWatch — pytest (backend, worker, upscaler), vitest (frontend), e2e на Playwright. Используй при добавлении функциональности или исправлении бага.
allowed-tools: Read, Write, Edit, Bash, Grep, Glob
---

# Написание тестов

Главное правило проекта (см. `e2e/README.md`): **тест проверяет контракт ТЗ, а не то поведение,
которое сейчас работает**. Тест на исправленный баг сначала должен падать на старом коде.

## Куда писать

| Что меняется | Где тест | Запуск |
|---|---|---|
| Эндпоинт / сервис backend | `backend/tests/test_<тема>.py` | `cd backend && .venv/bin/pytest -q tests/test_<тема>.py` |
| Работа с настоящей БД | `backend/tests/test_integration_<тема>.py` | нужны Postgres (pgvector:pg16) и Redis, `DATABASE_URL`, `REDIS_URL` |
| Миграция схемы | `backend/tests/test_startup_migrations.py` | настоящий Postgres |
| Worker | `worker/tests/test_<тема>.py` | `cd worker && pytest tests/ -q` |
| Живой MediaMTX | `worker/tests/test_<тема>_live.py` | `pytest tests/test_*_live.py -q` |
| Upscaler | `upscaler/tests/` | `cd upscaler && pytest tests/ -q` |
| Логика UI | `frontend/src/<модуль>.test.ts` рядом с модулем | `cd frontend && npm test` (vitest) |
| Действие оператора в браузере | `e2e/test_ui_actions.py` | `./e2e/run-stack.sh && pytest e2e -q` |

## Шаги

1. Найди требование в `SPEC.md` и сформулируй, что должно произойти с точки зрения ТЗ
2. Посмотри соседние тесты и фикстуры (`backend/tests/conftest.py`, `e2e/conftest.py`) — переиспользуй их
3. Напиши тест; для бага — убедись, что он падает без исправления
4. Запусти и проверь, что тест **не ушёл в skip** (`pytest -rs` показывает причины пропуска)
5. Для backend проверь покрытие: `.venv/bin/pytest --cov=app --cov-fail-under=77 -q`

## Чего избегать
- Проверять только код ответа, когда важен результат: скачанные байты, строка в БД, состояние после перезагрузки
- Слать запросы «как удобно тесту»: браузер шлёт `multipart/form-data`, а не urlencoded; у CSV для Excel нужен BOM
- Синтетические эмбеддинги там, где важна кластеризация настоящих лиц
- Статические проверки по тексту файла вместо проверки поведения

## Пример (backend, интеграционный)

```python
async def test_person_not_found_returns_404(client, admin_token):
    """SPEC §15: карточка несуществующей персоны — 404, а не 500"""
    r = await client.get("/api/persons/999999", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 404
```

Имена фикстур сверь с `backend/tests/conftest.py` — пример иллюстративный.
