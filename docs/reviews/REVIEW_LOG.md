# Журнал циклов аудита FaceWatch

| ID | Дата (UTC) | P0 найдено/исправлено | P1 найдено/исправлено | P2 найдено/исправлено | P3 найдено/исправлено | PR |
|---|---|---|---|---|---|---|
| REVIEW-2026-08-03T203014Z | 2026-08-03T20:30:14Z | 3/3 | 2/2 | не аудировался | не аудировался | [#3](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/3), [#4](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/4), [#5](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/5), [#6](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/6), [#7](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/7) |
| REVIEW-2026-08-03T224453Z | 2026-08-03T22:44:53Z | 2/2 | 1/1 | не аудировался | 1/1 | [#8](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/8), [#9](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/9), [#10](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/10), [#11](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/11) |
| REVIEW-2026-08-04T005000Z | 2026-08-04T00:50:00Z | 6/6 | 1/1 | аудирован (read-only), 1 пробел (ONVIF) | 1/1 | [#12](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/12), [#13](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/13), [#14](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/14), [#15](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/15), [#16](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/16) |
| REVIEW-2026-08-04T023249Z | 2026-08-04T02:32:49Z | 1/1 | 1/1 | не аудировался повторно | 1/1 | [#17](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/17), [#18](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/18), [#19](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/19) |
| REVIEW-2026-08-04T043001Z | 2026-08-04T04:30:01Z | не аудировался повторно | 1/1 (новый) | не аудировался повторно | 1/1 | [#20](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/20) |
| REVIEW-2026-08-04T064322Z | 2026-08-04T06:43:22Z | 1/1 (новый — /hls/ без авторизации) | 0/0 (целевая проверка чиста) | 1/1 (ONVIF-клиент, частично — без автообнаружения) | 1/1 | [#21](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/21), [#22](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/22), [#23](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/23), [#24](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/24) |

Подробности каждого цикла — в `docs/reviews/REVIEW-<UTC-timestamp>.md`.

## Известные пробелы, перенесённые в следующие циклы
(из REVIEW-2026-08-04T064322Z — актуально до следующего аудита)
- ONVIF WS-Discovery автообнаружение камер (остаток ТЗ 18.7) — клиент событий движения (PullPoint) реализован и протестирован в цикле 6 без реальной камеры (мокнутые SOAP-ответы); автообнаружение — широковещательный UDP multicast, сложнее осмысленно замокать, реально выигрывает от эмулятора (`onvif-simulator` в Docker)
- Удаление смерджённых веток `fix/*`/`docs/*`/`feat/*`/`test/*` (19+ штук за пять циклов, ещё 3 добавились в цикле 6 — `git push --delete` получает HTTP 403 от прокси песочницы шесть циклов подряд; отдельного `delete_branch` в доступных GitHub MCP-инструментах тоже нет; нужна ручная очистка или расширение прав на стороне оркестрации)
- Разночтение назначенной ветки харнесса vs фактической ветки с кодом на origin — устойчивый паттерн все шесть циклов (см. раздел «Разночтение окружения» в REVIEW-2026-08-04T064322Z.md), стоит проверить на стороне оркестрации сессий
- Полноценный проход по производительности не проводился с цикла 3 — если появятся сигналы деградации на реальном железе, стоит перезамерить FPS/CPU по профилям с текущим кодом
