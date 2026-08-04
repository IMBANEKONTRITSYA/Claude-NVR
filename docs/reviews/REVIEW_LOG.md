# Журнал циклов аудита FaceWatch

| ID | Дата (UTC) | P0 найдено/исправлено | P1 найдено/исправлено | P2 найдено/исправлено | P3 найдено/исправлено | PR |
|---|---|---|---|---|---|---|
| REVIEW-2026-08-03T203014Z | 2026-08-03T20:30:14Z | 3/3 | 2/2 | не аудировался | не аудировался | [#3](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/3), [#4](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/4), [#5](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/5), [#6](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/6), [#7](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/7) |
| REVIEW-2026-08-03T224453Z | 2026-08-03T22:44:53Z | 2/2 | 1/1 | не аудировался | 1/1 | [#8](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/8), [#9](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/9), [#10](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/10), [#11](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/11) |
| REVIEW-2026-08-04T005000Z | 2026-08-04T00:50:00Z | 6/6 | 1/1 | аудирован (read-only), 1 пробел (ONVIF) | 1/1 | [#12](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/12), [#13](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/13), [#14](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/14), [#15](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/15), [#16](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/16) |

Подробности каждого цикла — в `docs/reviews/REVIEW-<UTC-timestamp>.md`.

## Известные пробелы, перенесённые в следующие циклы
(из REVIEW-2026-08-04T005000Z — актуально до следующего аудита)
- Документация (INSTALL/USER_GUIDE/ADMIN_GUIDE/API_DOCS/ARCHITECTURE/CHANGELOG) — три цикла подряд откладывается (P3)
- ONVIF-интеграция, ТЗ 18.7 — единственный настоящий пробел P2, требует реальной ONVIF-камеры для проверки
- Интеграционные тесты для persons/search/reports/ws роутеров — последний шаг к честной цели покрытия ≥70% (сейчас 58.9%, P3/CI)
- JSON-форматтер для application-логов backend/worker (ротация размера уже закрыта на уровне Docker, P1)
- Удаление смерджённых веток `fix/*` (15 штук за три цикла — заблокировано правами git-прокси песочницы во всех трёх циклах, не архитектурой проекта)
