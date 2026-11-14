"""Минимальный SOAP-слой для ONVIF Profile G (SPEC §12 «ONVIF … Profile G
(хранение)»).

Здесь только то, что общее у четырёх сервисов Profile G (device/recording/
search/replay): пространства имён, разбор входящего конверта, проверка
WS-Security UsernameToken и сборка ответа/фолта. Доменная логика (какие
записи есть, их границы во времени) — в `onvif_profile_g.py`, маршруты — в
`routers/onvif.py`.

**Почему SOAP собирается строками, а не через фреймворк.** ONVIF —
SOAP 1.2 с жёстко заданными WSDL/XSD; серверных SOAP-фреймворков на Python,
которые бы приняли готовый ONVIF WSDL, в проекте нет, а тянуть spyne/zeep
ради четырёх операций — лишняя зависимость. Ответов немного, форма их
фиксирована схемой ONVIF, поэтому они собираются как шаблоны. Разбор входа
идёт через `defusedxml` (не stdlib `xml.etree`): конверт приходит из сети,
и stdlib раскрывает ENTITY из DOCTYPE — «billion laughs» (SPEC §14, OWASP).

**Границы ответственности по безопасности.** Модуль проверяет
WS-Security UsernameToken (Text и Digest) против отдельной ONVIF-учётки из
конфигурации — НЕ против таблицы `users`: пароли пользователей хранятся
bcrypt-хэшами, а UsernameToken Digest требует знания пароля в открытом
виде. Свежесть `Created` проверяется (окно ±5 минут), и с цикла 39 ведётся
кэш использованных nonce (`nonce_cache_key` здесь + Redis в
`routers/onvif.py`): одной проверки свежести мало — внутри пятиминутного
окна перехваченный заголовок принимался сколько угодно раз.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

import defusedxml.ElementTree as ET


# --- Пространства имён ------------------------------------------------------

NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsse": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd",
    "wsu": "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd",
    "tt": "http://www.onvif.org/ver10/schema",
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trc": "http://www.onvif.org/ver10/recording/wsdl",
    "tse": "http://www.onvif.org/ver10/search/wsdl",
    "trp": "http://www.onvif.org/ver10/replay/wsdl",
    "tt_search": "http://www.onvif.org/ver10/schema",
}

# Типы пароля WS-Security.
_PWD_DIGEST = ("http://docs.oasis-open.org/wss/2004/01/"
               "oasis-200401-wss-username-token-profile-1.0#PasswordDigest")
_PWD_TEXT = ("http://docs.oasis-open.org/wss/2004/01/"
             "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# Допустимый сдвиг часов между клиентом и сервером для WS-Security Created.
CLOCK_SKEW = timedelta(minutes=5)

# Сколько помнить использованный nonce. Токен с `Created` за пределами
# CLOCK_SKEW отбраковывается и без кэша, поэтому помнить дольше двойного
# окна бессмысленно: за его границей повтор уже не пройдёт проверку
# свежести. Меньше — оставляло бы щель ровно посередине.
NONCE_TTL_SEC = int(2 * CLOCK_SKEW.total_seconds())


def localname(tag: str) -> str:
    """`{ns}Name` → `Name`. Разбор ведётся по локальным именам, потому что
    клиенты по-разному раскладывают префиксы, а неймспейсы у ONVIF-элементов
    фиксированы схемой."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def find_local(elem, name: str):
    """Первый потомок (на любой глубине) с данным локальным именем."""
    for e in elem.iter():
        if localname(e.tag) == name:
            return e
    return None


def find_all_local(elem, name: str) -> list:
    return [e for e in elem.iter() if localname(e.tag) == name]


def text_of(elem, name: str, default: str | None = None) -> str | None:
    node = find_local(elem, name) if elem is not None else None
    if node is None or node.text is None:
        return default
    return node.text.strip()


class SoapError(Exception):
    """Ошибка обработки запроса, которую надо вернуть как SOAP Fault.

    `subcode` — код из пространства ONVIF ter: (например `ter:NotAuthorized`),
    `reason` — человекочитаемая причина. HTTP-код всегда 400 для Sender-фолтов
    и 500 для Receiver: ONVIF-клиенты читают тело, а не статус, но корректный
    статус помогает прокси и логам.
    """

    def __init__(self, subcode: str, reason: str, *, receiver: bool = False,
                 http_status: int | None = None):
        super().__init__(reason)
        self.subcode = subcode
        self.reason = reason
        self.receiver = receiver
        self.http_status = http_status or (500 if receiver else 400)


# --- Разбор конверта --------------------------------------------------------

def parse_envelope(raw: bytes):
    """Возвращает (Header|None, Body). Бросает SoapError при кривом XML."""
    try:
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001 — любой сбой разбора → Sender fault
        raise SoapError("ter:WellFormed", f"Некорректный XML: {exc}") from exc
    header = None
    body = None
    for child in root:
        ln = localname(child.tag)
        if ln == "Header":
            header = child
        elif ln == "Body":
            body = child
    if body is None:
        raise SoapError("ter:WellFormed", "В конверте нет Body")
    return header, body


def body_action(body):
    """Локальное имя первого дочернего элемента Body — имя операции ONVIF
    (`GetRecordingSummary`, `FindRecordings`, …)."""
    for child in body:
        return localname(child.tag)
    return None


# --- WS-Security ------------------------------------------------------------

@dataclass
class Credentials:
    username: str
    password: str


def _digest_matches(password: str, nonce_b64: str, created: str,
                    provided_b64: str) -> bool:
    try:
        nonce = base64.b64decode(nonce_b64)
    except Exception:  # noqa: BLE001
        return False
    digest = hashlib.sha1(nonce + created.encode("utf-8")
                          + password.encode("utf-8")).digest()
    expected = base64.b64encode(digest).decode("ascii")
    # constant-time сравнение: избегаем утечки по времени на подборе дайджеста.
    import hmac
    return hmac.compare_digest(expected, provided_b64.strip())


def _created_is_fresh(created: str, now: datetime | None = None) -> bool:
    if not created:
        # Без Created дайджест бессмыслен (нет второго слагаемого), но для
        # PasswordText Created необязателен — свежесть тогда не проверяется.
        return True
    now = now or datetime.now(timezone.utc)
    txt = created.strip().replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(txt)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return abs(now - ts) <= CLOCK_SKEW


def verify_security(header, expected: Credentials,
                    now: datetime | None = None) -> None:
    """Проверяет WS-Security UsernameToken в заголовке против ожидаемой
    учётки. Бросает SoapError('ter:NotAuthorized') при любой неудаче.

    Поддержаны оба типа пароля: PasswordText (сравнение в открытую) и
    PasswordDigest (SHA1(nonce+created+password), как требует ONVIF Core).
    Отсутствие токена, чужой логин, протухший Created — всё это отказ.
    """
    if header is None:
        raise SoapError("ter:NotAuthorized", "Требуется WS-Security UsernameToken")
    token = find_local(header, "UsernameToken")
    if token is None:
        raise SoapError("ter:NotAuthorized", "Требуется WS-Security UsernameToken")

    username = text_of(token, "Username")
    pwd_node = find_local(token, "Password")
    if not username or pwd_node is None or pwd_node.text is None:
        raise SoapError("ter:NotAuthorized", "Неполный UsernameToken")

    # Логин сверяется первым, но отказ по логину и по паролю неразличим для
    # клиента (единый reason) — чтобы перебор не отличал «нет такого
    # пользователя» от «неверный пароль».
    pwd_type = (pwd_node.get("Type") or _PWD_DIGEST).strip()
    nonce = text_of(token, "Nonce", "") or ""
    created = text_of(token, "Created", "") or ""

    ok = username == expected.username
    if pwd_type == _PWD_TEXT:
        import hmac
        ok = ok and hmac.compare_digest(pwd_node.text.strip(), expected.password)
    else:  # PasswordDigest (по умолчанию)
        ok = ok and _created_is_fresh(created, now) and _digest_matches(
            expected.password, nonce, created, pwd_node.text)

    if not ok:
        raise SoapError("ter:NotAuthorized", "Неверные учётные данные ONVIF")


def nonce_cache_key(header) -> str | None:
    """Ключ кэша использованных nonce для этого токена, либо None, если
    кэшировать нечего (PasswordText — в нём nonce нет вовсе).

    Зачем кэш. Проверка `Created` на свежесть (`_created_is_fresh`) режет
    повтор перехваченного UsernameToken **только через пять минут**. Внутри
    этого окна перехваченный заголовок принимается сколько угодно раз: для
    Profile G это чужой список камер объекта, границы архива и, если
    replay-источник сконфигурирован, ссылка на воспроизведение записи.
    ONVIF Core прямо требует помнить использованные nonce — до цикла 39
    этого не было.

    Ключ — SHA-256 от пары (nonce, Created), а не от одного nonce: клиент
    вправе переиспользовать nonce с новым Created (спецификация этого не
    запрещает), и ключ по одному nonce отбраковывал бы законные запросы
    такого клиента. Хэш, а не значения: nonce и Created попадают в Redis,
    который читают и другие процессы, а сами значения — часть материала
    дайджеста.

    Функция чистая: обращение к Redis — в вызывающем (`routers/onvif.py`),
    чтобы разбор и сеть тестировались порознь.
    """
    if header is None:
        return None
    token = find_local(header, "UsernameToken")
    if token is None:
        return None
    pwd_node = find_local(token, "Password")
    if pwd_node is None:
        return None
    if (pwd_node.get("Type") or _PWD_DIGEST).strip() == _PWD_TEXT:
        # PasswordText не содержит nonce; повтор такого заголовка не
        # отличим от нового запроса в принципе, и притворяться, что кэш
        # что-то даёт, нельзя. Защита здесь — TLS (§14), а не кэш.
        return None
    nonce = (text_of(token, "Nonce", "") or "").strip()
    created = (text_of(token, "Created", "") or "").strip()
    if not nonce:
        return None
    material = f"{nonce}|{created}".encode("utf-8")
    return "onvif:g:nonce:" + hashlib.sha256(material).hexdigest()


# --- Сборка ответа ----------------------------------------------------------

def xml_escape(value) -> str:
    s = "" if value is None else str(value)
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def iso_utc(dt: datetime) -> str:
    """datetime → ONVIF xs:dateTime в UTC с `Z`.

    Сегменты пишутся временем UTC, но naive (`TIMESTAMP WITHOUT TIME ZONE`);
    naive трактуется как UTC. Дробная часть отбрасывается — ONVIF-клиентам
    достаточно секундной точности, а разнобой в числе знаков после запятой
    ломал строгие парсеры.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc).replace(microsecond=0)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# Пространства имён, объявляемые на конверте один раз — во всех ответах.
_ENV_NS = " ".join(
    f'xmlns:{p}="{u}"' for p, u in NS.items() if p != "tt_search"
)


def envelope(body_inner: str) -> str:
    """Оборачивает готовое тело ответа в конверт SOAP 1.2."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<s:Envelope {_ENV_NS}>"
        "<s:Body>"
        f"{body_inner}"
        "</s:Body>"
        "</s:Envelope>"
    )


def fault(err: SoapError) -> str:
    """SOAP 1.2 Fault с ONVIF-подкодом."""
    role = "s:Receiver" if err.receiver else "s:Sender"
    return envelope(
        "<s:Fault>"
        f"<s:Code><s:Value>{role}</s:Value>"
        f"<s:Subcode><s:Value>{xml_escape(err.subcode)}</s:Value></s:Subcode></s:Code>"
        f"<s:Reason><s:Text xml:lang=\"en\">{xml_escape(err.reason)}</s:Text></s:Reason>"
        "</s:Fault>"
    )
