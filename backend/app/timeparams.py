"""Время во входных параметрах API — к виду, в котором оно лежит в БД.

Все столбцы времени проекта — `TIMESTAMP WITHOUT TIME ZONE` и хранят UTC.
FastAPI же отдаёт параметр `2026-10-28T00:00:00+05:00` (или `...Z`) как
**aware**-datetime, и сравнение его с naive-столбцом падает в asyncpg
(`can't subtract offset-naive and offset-aware datetimes`) — то есть 500
вместо выдачи. Так было на поиске по фото (DEPLOY_FINDINGS D11), в журнале
аудита и его выгрузке, в поиске сегментов архива: любой внешний клиент
§12, присылающий время по ISO 8601 с поясом, получал 500.

Нормализация стоит на границе — в типе параметра, — а не в каждом
обработчике: экспорт архива уже приводил время сам (`export.as_naive_utc`),
а соседние эндпоинты этого не делали, и разнобой прожил до развёртывания.
`test_time_params.py` следит, чтобы ни один роутер не объявлял голый
`datetime` во входных параметрах.
"""
from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator

from .services.export import as_naive_utc

# Обязательный параметр объявляется как `Annotated[NaiveUtc, Query()]`, а
# НЕ `NaiveUtc = Query(...)`: во втором виде FastAPI 0.14x молча теряет
# валидатор из Annotated, и aware-время доходит до обработчика как есть
# (проверено; `test_time_params.py` ловит это поведенчески).
#
# Время запроса, приведённое к naive-UTC. Naive на входе остаётся как есть:
# его смысл (UTC) задан контрактом API и не угадывается.
NaiveUtc = Annotated[datetime, AfterValidator(as_naive_utc)]
