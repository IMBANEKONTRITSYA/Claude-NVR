# Журнал циклов аудита FaceWatch

| ID | Дата (UTC) | P0 найдено/исправлено | P1 найдено/исправлено | P2 найдено/исправлено | P3 найдено/исправлено | PR |
|---|---|---|---|---|---|---|
| REVIEW-2026-08-03T203014Z | 2026-08-03T20:30:14Z | 3/3 | 2/2 | не аудировался | не аудировался | [#3](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/3), [#4](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/4), [#5](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/5), [#6](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/6), [#7](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/7) |

Подробности каждого цикла — в `docs/reviews/REVIEW-<UTC-timestamp>.md`.

## Известные пробелы, перенесённые в следующие циклы
(из REVIEW-2026-08-03T203014Z — актуально до следующего аудита)
- Refresh-токены / ротация JWT / server-side revocation (P0)
- TLS/HTTPS по умолчанию (P0)
- Автоматическая проверка уязвимостей зависимостей в CI (P0/CI)
- Ежедневный бэкап PostgreSQL (P1)
- Измерение покрытия тестами (P3/CI)
- Полный аудит P2 (раздел 18 SPEC.md) и P3 (документация)
