"""Минимальный ONVIF-клиент событий (ТЗ 18.7): подписка на PullPoint и
получение событий движения/детекции людей напрямую от камеры, вместо
постоянного MOG2-префильтра на CPU ("камера делает предобработку на своём
чипе" — максимальная экономия ресурсов). Плюс WS-Discovery автообнаружение
камер в сети и получение профилей потоков (третья часть ТЗ 18.7:
"автообнаружение камер в сети, получение профилей потоков").

Реализован через stdlib (urllib, hashlib, socket, xml.etree) без внешних
ONVIF-библиотек (onvif-zeep и аналоги тянут zeep/suds — тяжёлые
транзитивные зависимости, которых нет и не должно быть в требованиях
воркера). WS-Security UsernameToken (PasswordDigest) собирается вручную по
спецификации WS-Security 1.0.

Семь предыдущих циклов аудита откладывали автообнаружение с обоснованием
«нет реальной ONVIF-камеры/эмулятора в песочнице для проверки». Это верно
для end-to-end проверки на реальном оборудовании, но, как и с PullPoint
(создан циклом 6 по тому же принципу), не мешает написать и протестировать
сам протокольный клиент: сборка Probe-сообщения и разбор ProbeMatches —
чистые функции без сети, а сама UDP-рассылка тестируется через
внедряемую socket_factory (без реального multicast — GitHub Actions
раннеры и песочницы часто блокируют multicast, полагаться на него в тестах
не надёжно)."""
import base64
import hashlib
import os
import socket
import time
import urllib.error
import urllib.request
import uuid as _uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape as _xml_escape

_SOAP_ENV_NS = "http://www.w3.org/2003/05/soap-envelope"
_EVENTS_NS = "http://www.onvif.org/ver10/events/wsdl"
_MEDIA_NS = "http://www.onvif.org/ver10/media/wsdl"
_SCHEMA_NS = "http://www.onvif.org/ver10/schema"

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
        # Экранируем через xml.sax.saxutils.escape, как и profile_token в
        # get_stream_uri() ниже — username здесь вводится администратором в
        # форме камеры (OnvifProfilesRequest/CameraIn), тот же уровень
        # доверия, что и остальные поля CameraIn, но подстановка в XML без
        # экранирования всё равно даёт возможность выйти за пределы
        # <Username> и внедрить произвольные SOAP-элементы в запрос к самой
        # ONVIF-камере (например, значение вида
        # `foo</Username><Bogus>x` разваливает структуру заголовка) — цена
        # экранирования нулевая, а его отсутствие — единственное
        # непоследовательное место в модуле (password подставляется только
        # в SHA1-дайджест, а не в XML напрямую, поэтому не нуждается).
        f"<Username>{_xml_escape(username)}</Username>"
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


def get_profiles(
    host: str, port: int, username: str | None, password: str | None, timeout: float = 5.0,
) -> list[dict]:
    """GetProfiles ONVIF Media-сервиса: список медиа-профилей камеры
    (token + Name), из которых потом выбирается один для GetStreamUri —
    вторая недостающая часть ТЗ 18.7 ("получение профилей потоков").
    Путь сервиса — по тому же принципу, что и /onvif/Events для событий:
    большинство прошивок публикуют Media на конвенциональном /onvif/Media
    (полный вариант — резолвить XAddr через GetCapabilities/GetServices на
    device_service, но это отдельный раунд запросов ради адреса, который
    почти всегда один и тот же; при необходимости можно расширить, если
    реальная камера окажется нестандартной)."""
    url = f"http://{host}:{port}/onvif/Media"
    body = f'<GetProfiles xmlns="{_MEDIA_NS}"/>'
    raw = _post(url, _soap_envelope(body, username, password), timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise OnvifError(f"невалидный XML в ответе GetProfiles: {e}") from e
    profiles = []
    for p in _find_all(root, "Profiles"):
        token = p.attrib.get("token")
        if not token:
            continue
        name_elem = _find_one(p, "Name")
        name = (name_elem.text or "").strip() if name_elem is not None else None
        profiles.append({"token": token, "name": name or token})
    return profiles


def get_stream_uri(
    host: str, port: int, profile_token: str, username: str | None, password: str | None,
    timeout: float = 5.0,
) -> str:
    """GetStreamUri ONVIF Media-сервиса: RTSP-адрес RTP-Unicast потока для
    заданного профиля. profile_token приходит из ответа камеры (GetProfiles)
    и подставляется обратно в тело SOAP-запроса — экранируется через
    xml.sax.saxutils.escape, в отличие от username/password (вводятся
    администратором вручную в форме камеры, тот же уровень доверия, что и
    остальные поля CameraIn); token же — данные с сети, пусть и локальной,
    поэтому подставляется в XML безопасно."""
    url = f"http://{host}:{port}/onvif/Media"
    body = (
        f'<GetStreamUri xmlns="{_MEDIA_NS}">'
        "<StreamSetup>"
        f'<Stream xmlns="{_SCHEMA_NS}">RTP-Unicast</Stream>'
        f'<Transport xmlns="{_SCHEMA_NS}"><Protocol>RTSP</Protocol></Transport>'
        "</StreamSetup>"
        f"<ProfileToken>{_xml_escape(profile_token)}</ProfileToken>"
        "</GetStreamUri>"
    )
    raw = _post(url, _soap_envelope(body, username, password), timeout)
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise OnvifError(f"невалидный XML в ответе GetStreamUri: {e}") from e
    uri_elem = _find_one(root, "Uri")
    if uri_elem is None or not (uri_elem.text or "").strip():
        raise OnvifError("в ответе GetStreamUri нет адреса потока (Uri)")
    return uri_elem.text.strip()


_WSDD_NS = "http://schemas.xmlsoap.org/ws/2005/04/discovery"
_WSA_NS = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_WSDD_MULTICAST_ADDR = "239.255.255.250"
_WSDD_MULTICAST_PORT = 3702


def _probe_message() -> str:
    """SOAP-тело WS-Discovery Probe, разосланного multicast-датаграммой на
    239.255.255.250:3702 (спецификация WS-Discovery 1.1). Types ограничен
    dn:NetworkVideoTransmitter — ONVIF-профиль камеры/энкодера, а не любое
    WS-Discovery устройство в сети (принтеры и т.п. тоже на него отвечают)."""
    message_id = f"uuid:{_uuid.uuid4()}"
    return (
        '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
        f'xmlns:w="{_WSA_NS}" xmlns:d="{_WSDD_NS}" '
        'xmlns:dn="http://www.onvif.org/ver10/network/wsdl">'
        "<e:Header>"
        f"<w:MessageID>{message_id}</w:MessageID>"
        '<w:To e:mustUnderstand="1">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
        f'<w:Action e:mustUnderstand="1">{_WSDD_NS}/Probe</w:Action>'
        "</e:Header>"
        "<e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>"
        "</e:Envelope>"
    )


def _xaddr_host_port(xaddr: str) -> tuple[str, int] | None:
    """host_service_URL ('http://192.168.1.64/onvif/device_service') -> (host, port)."""
    parsed = urlsplit(xaddr)
    if not parsed.hostname:
        return None
    return parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)


def _parse_probe_matches(raw: bytes) -> list[dict]:
    """Разбирает один UDP-ответ ProbeMatches в список найденных устройств:
    {address, xaddrs, scopes, host, port}. Невалидный XML или пакет без
    XAddrs — пустой список, а не исключение: в общей сети могут отвечать
    другие WS-Discovery устройства (принтеры, NAS), не только ONVIF-камеры,
    и один битый/чужой пакет не должен ронять весь discover_devices()."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return []
    devices = []
    for match in _find_all(root, "ProbeMatch"):
        addr_elem = _find_one(match, "Address")
        address = (addr_elem.text or "").strip() if addr_elem is not None else ""
        xaddrs_elem = _find_one(match, "XAddrs")
        xaddrs = (xaddrs_elem.text or "").split() if xaddrs_elem is not None else []
        if not xaddrs:
            continue
        scopes_elem = _find_one(match, "Scopes")
        scopes = (scopes_elem.text or "").split() if scopes_elem is not None else []
        host_port = None
        for xaddr in xaddrs:
            host_port = _xaddr_host_port(xaddr)
            if host_port:
                break
        devices.append({
            "address": address,
            "xaddrs": xaddrs,
            "scopes": scopes,
            "host": host_port[0] if host_port else None,
            "port": host_port[1] if host_port else None,
        })
    return devices


def discover_devices(timeout: float = 3.0, socket_factory=socket.socket) -> list[dict]:
    """WS-Discovery: рассылает multicast Probe на 239.255.255.250:3702 и
    собирает ProbeMatch-ответы в течение timeout секунд. Возвращает список
    найденных устройств, без дублей по Address (камера может ответить с
    нескольких сетевых интерфейсов). socket_factory подменяется в тестах —
    сама UDP/multicast-рассылка не тестируется юнитом (см. docstring
    модуля), тестируется сборка запроса, разбор ответа и оркестрация вокруг
    сокета (дедупликация, уважение timeout, устойчивость к мусорным
    пакетам)."""
    sock = socket_factory(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(timeout)
        try:
            sock.sendto(_probe_message().encode("utf-8"), (_WSDD_MULTICAST_ADDR, _WSDD_MULTICAST_PORT))
        except OSError as e:
            raise OnvifError(f"не удалось отправить WS-Discovery Probe: {e}") from e

        devices_by_key: dict[str, dict] = {}
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                data, addr = sock.recvfrom(65535)
            except (socket.timeout, OSError):
                break
            for device in _parse_probe_matches(data):
                if device["host"] is None:
                    device["host"] = addr[0]
                key = device["address"] or f"{device['host']}:{device['port']}"
                devices_by_key.setdefault(key, device)
        return list(devices_by_key.values())
    finally:
        sock.close()


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
