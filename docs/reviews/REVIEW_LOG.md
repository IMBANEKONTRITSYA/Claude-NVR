# Журнал циклов аудита FaceWatch

| ID | Дата (UTC) | P0 найдено/исправлено | P1 найдено/исправлено | P2 найдено/исправлено | P3 найдено/исправлено | PR |
|---|---|---|---|---|---|---|
| REVIEW-2026-08-03T203014Z | 2026-08-03T20:30:14Z | 3/3 | 2/2 | не аудировался | не аудировался | [#3](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/3), [#4](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/4), [#5](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/5), [#6](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/6), [#7](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/7) |
| REVIEW-2026-08-03T224453Z | 2026-08-03T22:44:53Z | 2/2 | 1/1 | не аудировался | 1/1 | [#8](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/8), [#9](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/9), [#10](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/10), [#11](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/11) |
| REVIEW-2026-08-04T005000Z | 2026-08-04T00:50:00Z | 6/6 | 1/1 | аудирован (read-only), 1 пробел (ONVIF) | 1/1 | [#12](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/12), [#13](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/13), [#14](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/14), [#15](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/15), [#16](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/16) |
| REVIEW-2026-08-04T023249Z | 2026-08-04T02:32:49Z | 1/1 | 1/1 | не аудировался повторно | 1/1 | [#17](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/17), [#18](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/18), [#19](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/19) |

Подробности каждого цикла — в `docs/reviews/REVIEW-<UTC-timestamp>.md`.

## Известные пробелы, перенесённые в следующие циклы
(из REVIEW-2026-08-04T023249Z — актуально до следующего аудита)
- Интеграционные тесты для persons/search/reports/ws роутеров — последний шаг к честной цели покрытия ≥70% (сейчас 58.9%, P3/CI)
- ONVIF-интеграция, ТЗ 18.7 — единственный настоящий функциональный пробел, требует реальной ONVIF-камеры для проверки (четвёртый цикл подряд отложено по этой причине)
- Удаление смерджённых веток `fix/*`/`docs/*` (18 штук за четыре цикла — `git push --delete` получает HTTP 403 от прокси песочницы все четыре цикла подряд, не архитектурой проекта; нужна ручная очистка или расширение прав прокси)
- Разночтение назначенной ветки харнесса vs фактической ветки с кодом на origin — устойчивый паттерн все четыре цикла (см. раздел «Разночтение окружения» в REVIEW-2026-08-04T023249Z.md), стоит проверить на стороне оркестрации сессий
