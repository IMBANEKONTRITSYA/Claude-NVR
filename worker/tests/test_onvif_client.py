"""Юнит-тесты ONVIF-клиента (ТЗ 18.7) — чистая логика, без сети и без
реальной камеры: HTTP замокан через monkeypatch на urllib.request.urlopen,
SOAP-ответы — статичные XML-фикстуры по образцу спецификации ONVIF Core/
Events. Пять предыдущих циклов аудита откладывали ONVIF целиком, ссылаясь
на отсутствие камеры в песочнице — этот файл показывает, что протокольный
клиент можно проверить и без неё."""
import base64
import hashlib
import socket
from datetime import datetime, timezone

import pytest

import onvif_client as oc


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(monkeypatch, response_bytes: bytes | None = None, exc: Exception | None = None, capture: dict | None = None):
    def fake(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["body"] = req.data.decode("utf-8")
            capture["headers"] = dict(req.header_items())
            capture["timeout"] = timeout
        if exc is not None:
            raise exc
        return _FakeResponse(response_bytes)

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake)


# ---------------------------------------------------------------------------
# WS-Security UsernameToken (PasswordDigest)
# ---------------------------------------------------------------------------

def test_username_token_digest_matches_wssecurity_spec():
    header = oc._username_token_header("admin", "s3cret")
    assert "<Username>admin</Username>" in header
    assert 'Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest"' in header

    # Извлекаем Nonce/Created/Digest из сгенерированного заголовка и
    # пересчитываем digest независимо — должен совпасть 1-в-1 с формулой
    # спецификации: Base64(SHA1(RawNonce + Created + Password)).
    import re
    nonce_b64 = re.search(r"<Nonce[^>]*>([^<]+)</Nonce>", header).group(1)
    created = re.search(r"<wsu:Created>([^<]+)</wsu:Created>", header).group(1)
    digest = re.search(r"<Password[^>]*>([^<]+)</Password>", header).group(1)

    raw_nonce = base64.b64decode(nonce_b64)
    expected = base64.b64encode(
        hashlib.sha1(raw_nonce + created.encode() + b"s3cret").digest()
    ).decode()
    assert digest == expected

    # Created — валидный UTC ISO8601 в пределах разумного окна от "сейчас"
    parsed = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs((datetime.now(timezone.utc) - parsed).total_seconds()) < 30


def test_username_token_escapes_username_from_camera_form():
    # username вводится администратором в форме камеры (CameraIn/
    # OnvifProfilesRequest) — тот же уровень доверия, что и остальные поля
    # CameraIn, но без экранирования всё равно можно выйти за пределы
    # <Username> и внедрить посторонние SOAP-элементы в запрос к камере
    # (тот же класс проблемы, что profile_token в get_stream_uri() ниже —
    # там уже экранируется, здесь раньше не было).
    header = oc._username_token_header('"><Injected/>', "s3cret")
    assert "<Injected/>" not in header
    assert '"&gt;&lt;Injected/&gt;' in header


def test_envelope_without_credentials_has_empty_header():
    envelope = oc._soap_envelope("<Body/>", None, None)
    assert "<soap:Header></soap:Header>" in envelope
    assert "UsernameToken" not in envelope


# ---------------------------------------------------------------------------
# create_pull_point_subscription
# ---------------------------------------------------------------------------

CREATE_SUBSCRIPTION_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:wsa="http://www.w3.org/2005/08/addressing"
    xmlns:tev="http://www.onvif.org/ver10/events/wsdl">
  <SOAP-ENV:Body>
    <tev:CreatePullPointSubscriptionResponse>
      <tev:SubscriptionReference>
        <wsa:Address>http://192.168.1.64/onvif/Events/PullPoint/abc123</wsa:Address>
      </tev:SubscriptionReference>
      <wsnt:CurrentTime xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">2026-08-04T05:00:00Z</wsnt:CurrentTime>
    </tev:CreatePullPointSubscriptionResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def test_create_pull_point_subscription_parses_address(monkeypatch):
    capture = {}
    _mock_urlopen(monkeypatch, response_bytes=CREATE_SUBSCRIPTION_RESPONSE.encode(), capture=capture)
    url = oc.create_pull_point_subscription("192.168.1.64", 80, "admin", "s3cret")
    assert url == "http://192.168.1.64/onvif/Events/PullPoint/abc123"
    assert capture["url"] == "http://192.168.1.64:80/onvif/Events"
    assert "UsernameToken" in capture["body"]
    assert capture["headers"]["Content-type"] == "application/soap+xml; charset=utf-8"


def test_create_pull_point_subscription_without_credentials_omits_security(monkeypatch):
    capture = {}
    _mock_urlopen(monkeypatch, response_bytes=CREATE_SUBSCRIPTION_RESPONSE.encode(), capture=capture)
    oc.create_pull_point_subscription("192.168.1.64", 80, None, None)
    assert "UsernameToken" not in capture["body"]


def test_create_pull_point_subscription_raises_on_network_error(monkeypatch):
    import urllib.error
    _mock_urlopen(monkeypatch, exc=urllib.error.URLError("connection refused"))
    with pytest.raises(oc.OnvifError):
        oc.create_pull_point_subscription("192.168.1.64", 80, "admin", "s3cret")


def test_create_pull_point_subscription_raises_on_malformed_xml(monkeypatch):
    _mock_urlopen(monkeypatch, response_bytes=b"not xml at all")
    with pytest.raises(oc.OnvifError):
        oc.create_pull_point_subscription("192.168.1.64", 80, "admin", "s3cret")


def test_create_pull_point_subscription_raises_when_address_missing(monkeypatch):
    empty = """<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
    <SOAP-ENV:Body><tev:CreatePullPointSubscriptionResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl"/></SOAP-ENV:Body>
    </SOAP-ENV:Envelope>"""
    _mock_urlopen(monkeypatch, response_bytes=empty.encode())
    with pytest.raises(oc.OnvifError):
        oc.create_pull_point_subscription("192.168.1.64", 80, "admin", "s3cret")


# ---------------------------------------------------------------------------
# pull_messages
# ---------------------------------------------------------------------------

PULL_MESSAGES_RESPONSE_WITH_MOTION = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
    xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tev:PullMessagesResponse>
      <tev:CurrentTime>2026-08-04T05:00:10Z</tev:CurrentTime>
      <tev:TerminationTime>2026-08-04T05:10:00Z</tev:TerminationTime>
      <wsnt:NotificationMessage>
        <wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/CellMotionDetector/Motion</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="2026-08-04T05:00:09Z">
            <tt:Data>
              <tt:SimpleItem Name="State" Value="true"/>
            </tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
      <wsnt:NotificationMessage>
        <wsnt:Topic>tns1:VideoSource/GlobalSceneChange/ImagingService</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="2026-08-04T05:00:10Z">
            <tt:Data/>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>
    </tev:PullMessagesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def test_pull_messages_parses_topic_and_state(monkeypatch):
    _mock_urlopen(monkeypatch, response_bytes=PULL_MESSAGES_RESPONSE_WITH_MOTION.encode())
    events = oc.pull_messages("http://192.168.1.64/onvif/Events/PullPoint/abc123", "admin", "s3cret")
    assert len(events) == 2
    assert events[0]["topic"] == "tns1:RuleEngine/CellMotionDetector/Motion"
    assert events[0]["state"] == "true"
    assert events[0]["utc_time"] == "2026-08-04T05:00:09Z"
    assert events[1]["state"] is None


def test_pull_messages_empty_response_returns_empty_list(monkeypatch):
    empty = """<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
    <SOAP-ENV:Body><tev:PullMessagesResponse xmlns:tev="http://www.onvif.org/ver10/events/wsdl"/></SOAP-ENV:Body>
    </SOAP-ENV:Envelope>"""
    _mock_urlopen(monkeypatch, response_bytes=empty.encode())
    assert oc.pull_messages("http://cam/PullPoint/x", None, None) == []


def test_pull_messages_raises_on_timeout(monkeypatch):
    _mock_urlopen(monkeypatch, exc=TimeoutError("timed out"))
    with pytest.raises(oc.OnvifError):
        oc.pull_messages("http://cam/PullPoint/x", "admin", "s3cret")


def test_pull_messages_sends_timeout_and_limit_in_body(monkeypatch):
    capture = {}
    _mock_urlopen(monkeypatch, response_bytes=PULL_MESSAGES_RESPONSE_WITH_MOTION.encode(), capture=capture)
    oc.pull_messages("http://cam/PullPoint/x", "admin", "s3cret", timeout_sec=3, message_limit=10)
    assert "<Timeout>PT3S</Timeout>" in capture["body"]
    assert "<MessageLimit>10</MessageLimit>" in capture["body"]


# ---------------------------------------------------------------------------
# get_profiles / get_stream_uri (SPEC 18.7 — "получение профилей потоков")
# ---------------------------------------------------------------------------

GET_PROFILES_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetProfilesResponse>
      <trt:Profiles token="profile_1" fixed="true">
        <tt:Name>MainStream</tt:Name>
      </trt:Profiles>
      <trt:Profiles token="profile_2" fixed="true">
        <tt:Name>SubStream</tt:Name>
      </trt:Profiles>
    </trt:GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""

GET_STREAM_URI_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
    xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
    xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <trt:GetStreamUriResponse>
      <trt:MediaUri>
        <tt:Uri>rtsp://192.168.1.64:554/profile1</tt:Uri>
      </trt:MediaUri>
    </trt:GetStreamUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


def test_get_profiles_parses_token_and_name(monkeypatch):
    capture = {}
    _mock_urlopen(monkeypatch, response_bytes=GET_PROFILES_RESPONSE.encode(), capture=capture)
    profiles = oc.get_profiles("192.168.1.64", 80, "admin", "s3cret")
    assert profiles == [
        {"token": "profile_1", "name": "MainStream"},
        {"token": "profile_2", "name": "SubStream"},
    ]
    assert capture["url"] == "http://192.168.1.64:80/onvif/Media"
    assert "UsernameToken" in capture["body"]


def test_get_profiles_falls_back_to_token_when_name_missing(monkeypatch):
    no_name = """<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
    <SOAP-ENV:Body><trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl">
    <trt:Profiles token="profile_x"/></trt:GetProfilesResponse></SOAP-ENV:Body></SOAP-ENV:Envelope>"""
    _mock_urlopen(monkeypatch, response_bytes=no_name.encode())
    assert oc.get_profiles("192.168.1.64", 80, None, None) == [{"token": "profile_x", "name": "profile_x"}]


def test_get_profiles_empty_response_returns_empty_list(monkeypatch):
    empty = """<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
    <SOAP-ENV:Body><trt:GetProfilesResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/></SOAP-ENV:Body>
    </SOAP-ENV:Envelope>"""
    _mock_urlopen(monkeypatch, response_bytes=empty.encode())
    assert oc.get_profiles("192.168.1.64", 80, None, None) == []


def test_get_profiles_raises_on_malformed_xml(monkeypatch):
    _mock_urlopen(monkeypatch, response_bytes=b"not xml")
    with pytest.raises(oc.OnvifError):
        oc.get_profiles("192.168.1.64", 80, "admin", "s3cret")


def test_get_profiles_raises_on_network_error(monkeypatch):
    import urllib.error
    _mock_urlopen(monkeypatch, exc=urllib.error.URLError("connection refused"))
    with pytest.raises(oc.OnvifError):
        oc.get_profiles("192.168.1.64", 80, "admin", "s3cret")


def test_get_stream_uri_parses_uri(monkeypatch):
    capture = {}
    _mock_urlopen(monkeypatch, response_bytes=GET_STREAM_URI_RESPONSE.encode(), capture=capture)
    uri = oc.get_stream_uri("192.168.1.64", 80, "profile_1", "admin", "s3cret")
    assert uri == "rtsp://192.168.1.64:554/profile1"
    assert "<ProfileToken>profile_1</ProfileToken>" in capture["body"]
    assert "RTP-Unicast" in capture["body"]


def test_get_stream_uri_escapes_profile_token_from_camera(monkeypatch):
    # profile_token приходит из ответа камеры (GetProfiles) — потенциально
    # враждебный ввод (скомпрометированная/поддельная камера в локальной
    # сети), должен быть экранирован при подстановке обратно в XML-тело,
    # а не ломать структуру запроса или внедрять посторонние SOAP-элементы.
    capture = {}
    _mock_urlopen(monkeypatch, response_bytes=GET_STREAM_URI_RESPONSE.encode(), capture=capture)
    oc.get_stream_uri("192.168.1.64", 80, '"><Injected/>', "admin", "s3cret")
    assert "<Injected/>" not in capture["body"]
    assert '"&gt;&lt;Injected/&gt;' in capture["body"]


def test_get_stream_uri_raises_when_uri_missing(monkeypatch):
    empty = """<?xml version="1.0"?><SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
    <SOAP-ENV:Body><trt:GetStreamUriResponse xmlns:trt="http://www.onvif.org/ver10/media/wsdl"/></SOAP-ENV:Body>
    </SOAP-ENV:Envelope>"""
    _mock_urlopen(monkeypatch, response_bytes=empty.encode())
    with pytest.raises(oc.OnvifError):
        oc.get_stream_uri("192.168.1.64", 80, "profile_1", "admin", "s3cret")


def test_get_stream_uri_raises_on_timeout(monkeypatch):
    _mock_urlopen(monkeypatch, exc=TimeoutError("timed out"))
    with pytest.raises(oc.OnvifError):
        oc.get_stream_uri("192.168.1.64", 80, "profile_1", "admin", "s3cret")


# ---------------------------------------------------------------------------
# is_motion_event classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("topic,state,expected", [
    ("tns1:RuleEngine/CellMotionDetector/Motion", "true", True),
    ("tns1:RuleEngine/CellMotionDetector/Motion", "false", False),
    ("tns1:RuleEngine/CellMotionDetector/Motion", None, True),
    ("tns1:RuleEngine/PeopleDetector/People", "true", True),
    ("tns1:RuleEngine/PeopleDetector/People", "FALSE", False),  # регистронезависимо
    ("tns1:VideoSource/GlobalSceneChange/ImagingService", "true", False),
    ("", "true", False),
    (None, None, False),
])
def test_is_motion_event_classification(topic, state, expected):
    assert oc.is_motion_event(topic, state) is expected


# ---------------------------------------------------------------------------
# WS-Discovery: discover_devices (SPEC 18.7 — автообнаружение камер в сети)
# ---------------------------------------------------------------------------

def test_probe_message_has_discovery_action_and_type():
    body = oc._probe_message()
    assert f"{oc._WSDD_NS}/Probe" in body
    assert "dn:NetworkVideoTransmitter" in body
    assert "<w:MessageID>uuid:" in body


def test_probe_message_message_id_is_unique():
    assert oc._probe_message() != oc._probe_message()


PROBE_MATCH_RESPONSE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
    xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:resp-1</w:MessageID>
    <w:RelatesTo>uuid:probe-1</w:RelatesTo>
    <w:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</w:Action>
  </e:Header>
  <e:Body>
    <d:ProbeMatches>
      <d:ProbeMatch>
        <w:EndpointReference><w:Address>urn:uuid:11111111-2222-3333-4444-555555555555</w:Address></w:EndpointReference>
        <d:Types>dn:NetworkVideoTransmitter</d:Types>
        <d:Scopes>onvif://www.onvif.org/type/video_encoder onvif://www.onvif.org/name/HallwayCam</d:Scopes>
        <d:XAddrs>http://192.168.1.64/onvif/device_service</d:XAddrs>
        <d:MetadataVersion>1</d:MetadataVersion>
      </d:ProbeMatch>
    </d:ProbeMatches>
  </e:Body>
</e:Envelope>"""


def test_parse_probe_matches_extracts_device_fields():
    devices = oc._parse_probe_matches(PROBE_MATCH_RESPONSE.encode())
    assert len(devices) == 1
    d = devices[0]
    assert d["address"] == "urn:uuid:11111111-2222-3333-4444-555555555555"
    assert d["xaddrs"] == ["http://192.168.1.64/onvif/device_service"]
    assert d["scopes"] == [
        "onvif://www.onvif.org/type/video_encoder",
        "onvif://www.onvif.org/name/HallwayCam",
    ]
    assert d["host"] == "192.168.1.64"
    assert d["port"] == 80


def test_parse_probe_matches_malformed_xml_returns_empty():
    assert oc._parse_probe_matches(b"not xml") == []


def test_parse_probe_matches_without_xaddrs_is_skipped():
    no_xaddrs = """<?xml version="1.0"?>
    <e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
    <e:Body><d:ProbeMatches><d:ProbeMatch><d:Scopes>onvif://x</d:Scopes></d:ProbeMatch></d:ProbeMatches></e:Body>
    </e:Envelope>"""
    assert oc._parse_probe_matches(no_xaddrs.encode()) == []


@pytest.mark.parametrize("xaddr,expected", [
    ("http://192.168.1.64/onvif/device_service", ("192.168.1.64", 80)),
    ("http://192.168.1.64:8080/onvif/device_service", ("192.168.1.64", 8080)),
    ("https://192.168.1.64/onvif/device_service", ("192.168.1.64", 443)),
    ("not a url", None),
])
def test_xaddr_host_port(xaddr, expected):
    assert oc._xaddr_host_port(xaddr) == expected


class _FakeDiscoverySocket:
    """Подменяет реальный UDP/multicast-сокет: send_to больше нет, вместо
    сети — заранее заданный список (данные, адрес_отправителя), отдаваемый
    recvfrom() по одному, затем socket.timeout — как реальный сокет после
    settimeout() без новых пакетов."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.sent = []
        self.closed = False

    def setsockopt(self, *a, **kw):
        pass

    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        self.sent.append((data, addr))

    def recvfrom(self, bufsize):
        if self._responses:
            return self._responses.pop(0)
        raise socket.timeout()

    def close(self):
        self.closed = True


def test_discover_devices_returns_parsed_devices():
    fake = _FakeDiscoverySocket([(PROBE_MATCH_RESPONSE.encode(), ("192.168.1.64", 3702))])
    devices = oc.discover_devices(timeout=0.01, socket_factory=lambda *a, **kw: fake)
    assert len(devices) == 1
    assert devices[0]["host"] == "192.168.1.64"
    assert devices[0]["xaddrs"] == ["http://192.168.1.64/onvif/device_service"]
    # Probe разослан ровно один раз на multicast-адрес WS-Discovery
    assert len(fake.sent) == 1
    assert fake.sent[0][1] == (oc._WSDD_MULTICAST_ADDR, oc._WSDD_MULTICAST_PORT)
    assert fake.closed is True


def test_discover_devices_deduplicates_by_address():
    responses = [
        (PROBE_MATCH_RESPONSE.encode(), ("192.168.1.64", 3702)),
        (PROBE_MATCH_RESPONSE.encode(), ("192.168.1.64", 3702)),
    ]
    fake = _FakeDiscoverySocket(responses)
    devices = oc.discover_devices(timeout=0.01, socket_factory=lambda *a, **kw: fake)
    assert len(devices) == 1


def test_discover_devices_ignores_garbage_packets_from_other_wsdd_devices():
    responses = [
        (b"<garbage/>", ("192.168.1.5", 3702)),
        (PROBE_MATCH_RESPONSE.encode(), ("192.168.1.64", 3702)),
    ]
    fake = _FakeDiscoverySocket(responses)
    devices = oc.discover_devices(timeout=0.01, socket_factory=lambda *a, **kw: fake)
    assert len(devices) == 1
    assert devices[0]["host"] == "192.168.1.64"


def test_discover_devices_returns_empty_list_when_nothing_responds():
    fake = _FakeDiscoverySocket([])
    devices = oc.discover_devices(timeout=0.01, socket_factory=lambda *a, **kw: fake)
    assert devices == []
    assert fake.closed is True


def test_discover_devices_closes_socket_on_send_failure():
    class _FailingSocket(_FakeDiscoverySocket):
        def sendto(self, data, addr):
            raise OSError("network unreachable")

    fake = _FailingSocket([])
    with pytest.raises(oc.OnvifError):
        oc.discover_devices(timeout=0.01, socket_factory=lambda *a, **kw: fake)
    assert fake.closed is True
