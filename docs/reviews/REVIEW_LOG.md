# Журнал циклов аудита FaceWatch

| ID | Дата (UTC) | P0 найдено/исправлено | P1 найдено/исправлено | P2 найдено/исправлено | P3 найдено/исправлено | PR |
|---|---|---|---|---|---|---|
| REVIEW-2026-08-03T203014Z | 2026-08-03T20:30:14Z | 3/3 | 2/2 | не аудировался | не аудировался | [#3](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/3), [#4](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/4), [#5](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/5), [#6](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/6), [#7](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/7) |
| REVIEW-2026-08-03T224453Z | 2026-08-03T22:44:53Z | 2/2 | 1/1 | не аудировался | 1/1 | [#8](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/8), [#9](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/9), [#10](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/10), [#11](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/11) |
| REVIEW-2026-08-04T005000Z | 2026-08-04T00:50:00Z | 6/6 | 1/1 | аудирован (read-only), 1 пробел (ONVIF) | 1/1 | [#12](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/12), [#13](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/13), [#14](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/14), [#15](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/15), [#16](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/16) |
| REVIEW-2026-08-04T023249Z | 2026-08-04T02:32:49Z | 1/1 | 1/1 | не аудировался повторно | 1/1 | [#17](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/17), [#18](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/18), [#19](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/19) |
| REVIEW-2026-08-04T043001Z | 2026-08-04T04:30:01Z | не аудировался повторно | 1/1 (новый) | не аудировался повторно | 1/1 | [#20](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/20) |
| REVIEW-2026-08-04T064322Z | 2026-08-04T06:43:22Z | 1/1 (новый — /hls/ без авторизации) | 0/0 (целевая проверка чиста) | 1/1 (ONVIF-клиент, частично — без автообнаружения) | 1/1 | [#21](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/21), [#22](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/22), [#23](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/23), [#24](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/24) |
| REVIEW-2026-08-04T103308Z | 2026-08-04T10:33:08Z | 0/0 (целевая проверка чиста) | 3/3 (WS-сессии не перевалидировались, change-password без лимита попыток + скрытый 500 на настоящем Postgres) | не аудировался повторно | не аудировался повторно | [#25](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/25), [#26](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/26) |
| REVIEW-2026-08-04T142453Z | 2026-08-04T14:24:53Z | 0/0 (целевая проверка чиста) | 1/1 (раскрытие `?token=` в access-логи nginx/uvicorn — carryover цикла 7) | 1/1 (ONVIF WS-Discovery автообнаружение, SPEC 18.7 — последний открытый пункт) | не аудировался повторно | [#27](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/27), [#28](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/28) |
| REVIEW-2026-08-04T161845Z | 2026-08-04T16:18:45Z | не аудировался повторно | не аудировался повторно | 3/3 (полный пересмотр раздела 18 ТЗ, не проводился с цикла 3: битрейт/I-frame-only записи, низкий приоритет апскейлера, пул соединений БД) | 0/0 | [#29](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/29) |
| REVIEW-2026-08-04T163032Z | 2026-08-04T16:30:32Z | не аудировался повторно | не аудировался повторно | 1/1 (ONVIF получение профилей потоков — GetProfiles/GetStreamUri, финальный пункт ТЗ 18.7) | 0/0 | [#30](https://github.com/IMBANEKONTRITSYA/Claude-NVR/pull/30) |

Подробности каждого цикла — в `docs/reviews/REVIEW-<UTC-timestamp>.md`.

## ⚠️ Обнаружен конкурентный запуск сессий (циклы 9-10)
Цикл 9 нашёл прямые доказательства минимум двух независимых автоматических
сессий, работавших на `claude/pensive-brown-28588t` **одновременно** в одном
временном окне — обе датировали себя «циклом 8», обе описывают работу над
пересекающимися PR (#27/#28) от первого лица с деталями, которые не
подделать постфактум. Цикл 10 не нашёл новых прямых улик, но стартовал с
уже открытым и готовым (не самим им созданным) PR #29 — состояние,
согласующееся с тем же паттерном. Подробности и рекомендация — раздел
«Конкурентный запуск» в `REVIEW-2026-08-04T161845Z.md` (полное описание) и
`REVIEW-2026-08-04T163032Z.md` (подтверждение). На этот раз обошлось без
дублирующих PR или конфликтов мерджа, но это везение, не гарантия —
приоритет №1 для стороны оркестрации на следующий цикл.

## Известные пробелы, перенесённые в следующие циклы
(из REVIEW-2026-08-04T163032Z — актуально до следующего аудита)
- **Конкурентный запуск сессий на одной ветке** — см. предупреждение выше, приоритет №1
- 18.2 — аппаратное ускорение декодирования видео (GPU hwaccel для ffmpeg/OpenCV) не реализовано; инференс-провайдер (CUDA/DirectML/OpenVINO/CPU) уже автоопределяется, декодирование — нет; нужен цикл с реальным GPU для содержательной проверки
- Удаление смерджённых веток `fix/*`/`docs/*`/`feat/*`/`test/*` (19+ штук за пять циклов, ещё 3 в цикле 6, ещё 2 в цикле 7, ещё 2 в цикле 8 — `git push --delete` получает HTTP 403 от прокси песочницы десять циклов подряд; отдельного `delete_branch` в доступных GitHub MCP-инструментах тоже нет; нужна ручная очистка или расширение прав на стороне оркестрации — циклы 7-10 не повторяли попытку сверх однократной проверки, см. рекомендацию цикла 6 прекратить бесполезные повторы)
- Разночтение назначенной ветки харнесса vs фактической ветки с кодом на origin — устойчивый паттерн все десять циклов, стоит проверить на стороне оркестрации сессий
- Docker daemon недоступен пятый цикл подряд (циклы 6-10); Postgres+pgvector+Redis поднимаются напрямую в песочнице (`postgresql-16-pgvector` + системный `redis-server`) вместо `docker-compose` — сработало эквивалентно все пять раз (в цикле 10 пакет `postgresql-16-pgvector` пришлось ставить заново — похоже, песочница не переиспользуется между циклами), стоит задокументировать как официальный fallback, если это не разовая случайность окружения
- SPEC 18.7 (ONVIF) теперь закрыт полностью (события движения, автообнаружение, профили потоков) — следующим циклам не нужно возвращаться к этому разделу, кроме упомянутого выше 18.2 (декодирование)
