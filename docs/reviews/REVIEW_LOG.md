# Журнал циклов аудита FaceWatch

| ID | Дата (UTC) | P0 найдено/исправлено | P1 найдено/исправлено | P2 найдено/исправлено | P3 найдено/исправлено | PR |
|---|---|---|---|---|---|---|
| REVIEW-2026-08-03T203014Z | 2026-08-03T20:30:14Z | 3/3 | 2/2 | не аудировался | не аудировался | [#3](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/3), [#4](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/4), [#5](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/5), [#6](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/6), [#7](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/7) |
| REVIEW-2026-08-03T224453Z | 2026-08-03T22:44:53Z | 2/2 | 1/1 | не аудировался | 1/1 | [#8](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/8), [#9](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/9), [#10](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/10), [#11](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/11) |

Подробности каждого цикла — в `docs/reviews/REVIEW-<UTC-timestamp>.md`.

## Известные пробелы, перенесённые в следующие циклы
(из REVIEW-2026-08-03T224453Z — актуально до следующего аудита)
- TLS/HTTPS по умолчанию (P0)
- Парольная политика (сложность/срок действия) и авто-logout по неактивности (P0)
- Интеграционные тесты backend с реальным Postgres/Redis в CI — нужны, чтобы честно поднять покрытие к цели ≥70% (P3/CI)
- Полный аудит P2 (раздел 18 SPEC.md) и P3 (документация: INSTALL/USER_GUIDE/ADMIN_GUIDE/API_DOCS/ARCHITECTURE/CHANGELOG)
- Удаление смерджённых веток `fix/*` (заблокировано правами git-прокси песочницы в обоих циклах, не архитектурой проекта)
