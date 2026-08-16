"""Импорт и экспорт конфигурации камер (SPEC §3: «Импорт/экспорт
конфигурации камер (CSV/JSON)»).

Зачем это в NVR. §1 новой редакции ТЗ снял фиксированное «120 камер» и
задал диапазон 12–250+. Заводить две сотни камер по одной через форму —
это часы работы монтажника и неизбежные опечатки в RTSP-URL, а перенос
конфигурации с тестового стенда на боевой сервер без файла невозможен в
принципе. Промышленные NVR (Hikvision, Dahua) отдают такой файл штатно,
поэтому §3 требует того же.

**Ключ строки — имя камеры.** В БД оно не уникально, но в файле
конфигурации это единственное поле, которое человек читает и пишет
осознанно; `id` при переносе между установками не совпадёт, а RTSP-URL
меняется при перенастройке камеры. Поэтому импорт делает upsert по имени:
строка с известным именем обновляет камеру, с новым — заводит.

**Пароли по умолчанию не выгружаются.** RTSP-URL несёт учётку камеры, и
§14 требует хранить её только зашифрованной (AES-256). Файл, который
монтажник кладёт в почту или в git репозитория объекта, не должен
раздавать доступ ко всем камерам здания. Поэтому пароль в URL по
умолчанию заменяется на `***`, а полную выгрузку админ запрашивает явно
(`include_secrets=1`) — и она пишется в журнал аудита отдельным действием.
Пароль ONVIF не выгружается никогда: в отличие от RTSP-URL он не нужен
для опознания камеры в файле, а `GET /api/cameras/{id}/rtsp` для него
аналога не имеет.

**Маскированный URL при импорте — не ошибка формата, а сохранение
прежнего значения.** Круговой сценарий «выгрузил → поправил локацию →
загрузил обратно» обязан работать без ручного вписывания паролей.
Поэтому для существующей камеры `***` означает «оставить сохранённый
URL», и только для новой камеры это ошибка: подставить пароль неоткуда.
"""
from __future__ import annotations

import csv
import io
import json
from urllib.parse import urlsplit, urlunsplit

# Порядок колонок CSV и ключей JSON. Ключи машинные (латиница), а не
# «человеческие» заголовки отчётов из routers/reports.py: этот файл не
# читают глазами один раз, его правят и загружают обратно, и заголовок
# должен однозначно ложиться на поле API (CameraIn), без таблицы
# соответствий и без сюрпризов с кодировкой в Excel.
FIELDS = [
    "name",
    "location",
    "enabled",
    "mode",
    "rtsp_url",
    "sub_rtsp_url",
    "motion_sensitivity",
    "retention_days",
    "onvif_enabled",
    "onvif_host",
    "onvif_port",
    "onvif_username",
]

# Чем заменяется пароль в выгрузке без секретов.
MASK = "***"

_TRUE = {"1", "true", "yes", "y", "on", "да", "истина"}
_FALSE = {"0", "false", "no", "n", "off", "нет", "ложь", ""}


class ImportError_(ValueError):
    """Ошибка разбора файла целиком (не отдельной строки)."""


def mask_rtsp_url(url: str | None) -> str | None:
    """Заменяет пароль в RTSP-URL на `***`, остальное оставляет как есть.

    Логин сохраняется намеренно: без него строка перестаёт опознаваться
    как «та самая камера» при сверке файла с оборудованием, а сам по себе
    логин доступа не даёт.
    """
    if not url:
        return url
    parts = urlsplit(url)
    if not parts.password:
        return url
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    userinfo = f"{parts.username or ''}:{MASK}"
    return urlunsplit((parts.scheme, f"{userinfo}@{host}", parts.path, parts.query, parts.fragment))


def is_masked(url: str | None) -> bool:
    """True, если пароль в URL вырезан выгрузкой (значение `***`)."""
    if not url:
        return False
    try:
        return urlsplit(url).password == MASK
    except ValueError:
        return False


def camera_row(cam, main_url: str | None, sub_url: str | None, *, include_secrets: bool) -> dict:
    """Строка выгрузки по камере. URL передаются уже расшифрованными."""
    return {
        "name": cam.name,
        "location": cam.location or "",
        "enabled": bool(cam.enabled),
        "mode": cam.mode or "record_only",
        "rtsp_url": main_url if include_secrets else mask_rtsp_url(main_url),
        "sub_rtsp_url": (sub_url if include_secrets else mask_rtsp_url(sub_url)) or "",
        "motion_sensitivity": cam.motion_sensitivity,
        "retention_days": cam.retention_days,
        "onvif_enabled": bool(cam.onvif_enabled),
        "onvif_host": cam.onvif_host or "",
        "onvif_port": cam.onvif_port,
        "onvif_username": cam.onvif_username or "",
    }


def rows_to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
    w.writeheader()
    for row in rows:
        w.writerow({k: _csv_value(row.get(k)) for k in FIELDS})
    return buf.getvalue()


def rows_to_json(rows: list[dict]) -> str:
    return json.dumps({"cameras": rows}, ensure_ascii=False, indent=2)


def _csv_value(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "true" if v else "false"
    return v


def parse_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return default if s == "" else False
    raise ValueError(f"ожидалось да/нет, получено {v!r}")


def parse_int(v, field: str) -> int | None:
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return int(s)
    except ValueError:
        raise ValueError(f"поле {field}: ожидалось целое число, получено {v!r}") from None


def parse_file(content: bytes, filename: str = "") -> list[dict]:
    """Разбирает выгруженный файл в список сырых строк (значения — как в файле).

    Формат определяется по содержимому, а не по расширению: файл может
    приехать из другой системы с любым именем, а перепутать CSV и JSON
    после первого непробельного символа невозможно.
    """
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("файл не в кодировке UTF-8") from None
    stripped = text.lstrip()
    if not stripped:
        raise ImportError_("файл пуст")
    if stripped[0] in "{[":
        return _parse_json(stripped)
    return _parse_csv(text)


def _parse_json(text: str) -> list[dict]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportError_(f"некорректный JSON: {e}") from None
    if isinstance(data, dict):
        data = data.get("cameras")
    if not isinstance(data, list):
        raise ImportError_("ожидался список камер или объект с ключом «cameras»")
    rows = []
    for i, item in enumerate(data, start=1):
        if not isinstance(item, dict):
            raise ImportError_(f"элемент {i} не является объектом камеры")
        rows.append(item)
    return rows


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportError_("в CSV нет заголовка колонок")
    header = {(f or "").strip() for f in reader.fieldnames}
    if "name" not in header:
        raise ImportError_(
            "в заголовке CSV нет обязательной колонки «name»; "
            f"ожидались колонки: {', '.join(FIELDS)}"
        )
    rows = []
    for raw in reader:
        row = {(k or "").strip(): v for k, v in raw.items() if k is not None}
        # Пустые строки в конце файла (лишний перевод строки из Excel).
        if not any((str(v).strip() if v is not None else "") for v in row.values()):
            continue
        rows.append(row)
    return rows
