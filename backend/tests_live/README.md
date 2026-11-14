# Живые тесты бэкенда

Тесты, которым нужен настоящий MediaMTX (`MEDIAMTX_BIN`) и не нужна база.
Лежат вне `tests/` намеренно: `tests/conftest.py` поднимает приложение и
Postgres autouse-фикстурами, и здесь они превратили бы тест в молчаливый skip
на раннере без базы (джоба CI `record-layer-live`, где есть MediaMTX, но нет
Postgres). Запуск: `cd backend && pytest tests_live -q -rs`.
