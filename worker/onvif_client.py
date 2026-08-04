"""Минимальный ONVIF-клиент событий (ТЗ 18.7): подписка на PullPoint и
получение событий движения/детекции людей напрямую от камеры, вместо
постоянного MOG2-префильтра на CPU ("камера делает предобработку на своём
чипе" — максимальная экономия ресурсов).

Реализован через stdlib (urllib, hashlib, xml.etree) без внешних
ONVIF-библиотек (onvif-zeep и аналоги тянут zeep/suds — тяжёлые
транзитивные зависимости, которых нет и не должно быть в требованиях
воркера). WS-Security UsernameToken (PasswordDigest) собирается вручную по
спецификации WS-Security 1.0.

Пять предыдущих циклов аудита откладывали ONVIF целиком с обоснованием
«нет реальной ONVIF-камеры в песочнице для проверки». Это верно для
end-to-end проверки полного пользовательского сценария, но не мешает
написать и протестировать сам протокольный клиент — вся логика ниже
проверяется юнит-тестами на замоканных SOAP-ответах (test_onvif_client.py),
без сети и без реального устройства."""
import base64
import hashlib
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

_SOAP_ENV_NS = "http://www.w3.org/2003/05/soap-envelope"
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"

# Топики, соответствующие движению/присутствию людей в терминах ONVIF Event
# Topic Namespace (tns1:...) — покрывает основные профили G/S детекторов.
MOTION_TOPIC_KEYWORDS = (
    "motion", "peopledetector", "humandetector",
    "linedetector", "fielddetector", "visitorcounter",
)


class OnvifError(Exception):
    """Любая ошибка ONVIF-запроса: сеть, таймаут, невалидный SOAP-ответ."""


def _local(tag: str) -> str:
    """Имя тега без namespace-префикса ({ns}Tag -> Tag) — ONVIF-камеры
    используют разные префиксы (tev/wsnt/tt) для одних и тех же элементов,
    парсинг по локальному имени устойчивее, чем жёсткая привязка к префиксу."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _find_all(elem: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in elem.iter() if _local(e.tag) == name]


def _find_one(elem: ET.Element, name: str) -> ET.Element | None:
    found = _find_all(elem, name)
    return found[0] if found else None


def _username_token_header(username: str, password: str) -> str:
    """WS-Security UsernameToken с PasswordDigest (спецификация WS-Security
    UsernameToken Profile 1.0): Digest = Base64(SHA1(Nonce + Created + Password)),
    где Nonce — сырые случайные байты (не сам base64), Created — ISO8601 UTC."""
    nonce = os.urandom(16)
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    nonce_b64 = base64.b64encode(nonce).decode()
    return (
        '<Security xmlns="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        "<UsernameToken>"
        f"<Username>{username}</Username>"
        '<Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">'
        f"{digest}</Password>"
        '<Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">'
        f"{nonce_b64}</Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</UsernameToken>"
        "</Security>"
    )


def _soap_envelope(body: str, username: str | None, password: str | None) -> str:
    header = _username_token_header(username, password) if username else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<soap:Envelope xmlns:soap="{_SOAP_ENV_NS}">'
        f"<soap:Header>{header}</soap:Header>"
        f"<soap:Body>{body}</soap:Body>"
        "</soap:Envelope>"
    )


def _post(url: str, envelope: str, timeout: float) -> bytes:
    req = urllib.request.Request(
        url,
        data=envelope.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/soap+xml; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — ONVIF-камера в локальной сети, не произвольный URL из интернета
            return resp.read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise OnvifError(f"ONVIF-запрос к {url} не удался: {e}") from e


def create_pull_point_subscription(
    host: str, port: int, username: str | None, password: str | None, timeout: float = 5.0,
) -> str:
    """Создаёт подписку PullPoint и возвращает URL, по которому забирать
    события (PullMessages). Бросает OnvifError на любую сетевую/протокольную
    проблему — вызывающий код (onvif_poll_worker в worker.py) сам решает,
    ретраить ли и с какой задержкой."""
    url = f"http://{host}:{port}/onvif/Events"
    body = f'<CreatePullPointSubscription xmlns="{_EVENTS_NS}"/>'
    raw = _post(url, _soap_envelope(body, username, password), timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise OnvifError(f"невалидный XML в ответе CreatePullPointSubscription: {e}") from e
    addr_elem = _find_one(root, "Address")
    if addr_elem is None or not (addr_elem.text or "").strip():
        raise OnvifError("в ответе CreatePullPointSubscription нет адреса подписки (Address)")
    return addr_elem.text.strip()


def pull_messages(
    subscription_url: str,
    username: str | None,
    password: str | None,
    timeout_sec: int = 2,
    message_limit: int = 25,
    http_timeout: float = 8.0,
) -> list[dict]:
    """Забирает накопившиеся события с PullPoint. timeout_sec — long-poll
    таймаут на стороне камеры (сколько ждать хотя бы одно событие перед
    пустым ответом), http_timeout — таймаут самого HTTP-запроса (с запасом
    сверх timeout_sec, иначе клиент отваливается раньше камеры)."""
    body = (
        f'<PullMessages xmlns="{_EVENTS_NS}">'
        f"<Timeout>PT{int(timeout_sec)}S</Timeout>"
        f"<MessageLimit>{int(message_limit)}</MessageLimit>"
        "</PullMessages>"
    )
    raw = _post(subscription_url, _soap_envelope(body, username, password), http_timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise OnvifError(f"невалидный XML в ответе PullMessages: {e}") from e

    events = []
    for note in _find_all(root, "NotificationMessage"):
        topic_elem = _find_one(note, "Topic")
        topic = (topic_elem.text or "").strip() if topic_elem is not None else ""
        utc_time = None
        state = None
        # wsnt:NotificationMessage/wsnt:Message оборачивает tt:Message —
        # оба элемента называются "Message" без учёта namespace, а нужный
        # (с UtcTime и вложенными SimpleItem) всегда самый глубокий, то есть
        # последний в порядке pre-order обхода iter().
        message_candidates = _find_all(note, "Message")
        message_elem = message_candidates[-1] if message_candidates else None
        if message_elem is not None:
            utc_time = message_elem.attrib.get("UtcTime")
            for item in _find_all(message_elem, "SimpleItem"):
                if item.attrib.get("Name") == "State":
                    state = item.attrib.get("Value")
        events.append({"topic": topic, "utc_time": utc_time, "state": state})
    return events


def is_motion_event(topic: str | None, state: str | None = None) -> bool:
    """Топик из ONVIF Event Topic Namespace классифицируется как движение/
    присутствие человека, а State (если камера его передаёт) не равен
    "false" — некоторые камеры шлют парные события начало/конец движения
    с одним и тем же топиком и State=true/false."""
    t = (topic or "").lower()
    if not any(k in t for k in MOTION_TOPIC_KEYWORDS):
        return False
    if state is not None and state.strip().lower() == "false":
        return False
    return True
