"""Часовой пояс, в котором интерфейс показывает и принимает время.

Хранение не меняется: все столбцы времени — UTC без пояса, и API отдаёт и
принимает время в UTC. Пояс — настройка представления: интерфейс переводит
UTC в него при показе и обратно при вводе фильтров.

Зачем это понадобилось. До этого интерфейс выводил время через
`new Date(ts).toLocaleString()`, а строку без суффикса пояса браузер читает
как МЕСТНУЮ. В итоге архив и журнал аудита показывали цифры UTC под видом
местного времени: на объекте в UTC+5 запись «в 15:00» значилась как 10:00,
а фильтр «с 15:00» искал с 15:00 UTC — на пять часов позже. Стена
распознавания по той же причине писала про свежее лицо «5 ч назад».

Начальное значение берётся из системы при первом запуске (сид настроек в
main.py) — дальше пояс меняет администратор в настройках. Порядок
источников — от явного к угадываемому:

1. `FACEWATCH_TIMEZONE` — задан установщиком/`.env` (на Windows Docker
   контейнер иначе не видит пояса хоста: generate_env.ps1 пишет его сюда);
2. `TZ` — если это имя IANA, а не POSIX-строка вроде `UTC-5`;
3. `/etc/timezone` (Debian/Ubuntu — production-цель §26);
4. цель симлинка `/etc/localtime` → `.../zoneinfo/Asia/Yekaterinburg`;
5. `UTC`.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_TIMEZONE = "UTC"


def is_valid_timezone(name: str | None) -> bool:
    """Имя пояса IANA, известное базе zoneinfo (`Europe/Moscow`, `UTC`)."""
    if not name or not isinstance(name, str) or name != name.strip():
        return False
    # ZoneInfo принимает и пути вида "../../etc/passwd" как ключ поиска
    # файла — имя из запроса пользователя проверяется до обращения к нему.
    if name.startswith("/") or ".." in name or "\\" in name:
        return False
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return False
    return True


def _from_localtime_link(path: str = "/etc/localtime") -> str | None:
    try:
        target = os.path.realpath(path)
    except OSError:
        return None
    marker = "/zoneinfo/"
    if marker not in target:
        return None
    name = target.split(marker, 1)[1]
    # posix/ и right/ — альтернативные деревья той же базы.
    for prefix in ("posix/", "right/"):
        if name.startswith(prefix):
            name = name[len(prefix):]
    return name


def detect_system_timezone(environ=os.environ, etc_timezone: str = "/etc/timezone",
                           localtime: str = "/etc/localtime") -> str:
    """Пояс системы для начального значения настройки; `UTC`, если неясно."""
    candidates = [environ.get("FACEWATCH_TIMEZONE"), environ.get("TZ")]
    try:
        with open(etc_timezone, encoding="utf-8") as fh:
            candidates.append(fh.read().strip())
    except OSError:
        pass
    candidates.append(_from_localtime_link(localtime))
    for name in candidates:
        if name:
            name = name.strip().lstrip(":")  # TZ=":Europe/Moscow" — допустимая форма
        if is_valid_timezone(name):
            return name
    return DEFAULT_TIMEZONE


# --- Время в файлах, которые уходят за пределы интерфейса --------------------
#
# Выгрузки CSV/Excel и письма планировщика читают люди без интерфейса под
# рукой: столбец «Время» в UTC без пометки на объекте в UTC+5 читается как
# местное и сдвигает каждое событие на пять часов. Поэтому туда время идёт
# уже в поясе объекта, а пояс назван в заголовке столбца.

EXPORT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def zone(name: str | None) -> ZoneInfo:
    return ZoneInfo(name if is_valid_timezone(name) else DEFAULT_TIMEZONE)


async def load_display_timezone(db) -> str:
    """Пояс объекта из настроек; `UTC`, если он не задан или испорчен."""
    from sqlalchemy import select

    from ..models import Setting

    value = (await db.execute(
        select(Setting.value).where(Setting.key == "display_timezone")
    )).scalar_one_or_none()
    return value if is_valid_timezone(value) else DEFAULT_TIMEZONE


def utc_to_local(ts: datetime, tz: str) -> datetime:
    """UTC без пояса → стенное время объекта без пояса."""
    return ts.replace(tzinfo=timezone.utc).astimezone(zone(tz)).replace(tzinfo=None)


def local_to_utc(ts: datetime, tz: str) -> datetime:
    """Стенное время объекта без пояса → UTC без пояса.

    Несуществующее время перевода часов (02:30 весной) и двойное (осенью)
    разрешаются по fold=0 — как у zoneinfo по умолчанию: отчёт уйдёт в
    один из двух моментов, а не пропадёт.
    """
    return ts.replace(tzinfo=zone(tz)).astimezone(timezone.utc).replace(tzinfo=None)


def format_local(ts: datetime | None, tz: str) -> str:
    return utc_to_local(ts, tz).strftime(EXPORT_TIME_FORMAT) if ts else ""


def zone_header(title: str, tz: str) -> str:
    """«Время» → «Время (Asia/Yekaterinburg)»."""
    return f"{title} ({tz})"
