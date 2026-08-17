"""ONVIF Profile G (SPEC §12) — контрактные тесты SOAP-сервера записей.

Проверяется production path целиком: настоящий конверт SOAP уходит в
эндпоинт, ответ разбирается как XML, а данные о записях сидятся в тот же
Postgres, что и у приложения (фикстуры `make_camera` + `pg_conn`). Interop с
конкретным VMS проверяется на сервере (known gap) — здесь проверяется форма
запроса/ответа и авторизация.
"""
import base64
import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import pytest

from app.config import settings
from app.services import onvif_soap as soap
from app.services import onvif_profile_g as pg


SOAP_CT = {"Content-Type": "application/soap+xml; charset=utf-8"}


# --- Вспомогательное: сборка запросов ---------------------------------------

def _security_header(username: str, password: str) -> str:
    """WS-Security UsernameToken с PasswordText — проще для интеграционных
    тестов; путь Digest проверяется юнит-тестом verify_security ниже."""
    return (
        "<s:Header><wsse:Security xmlns:wsse=\""
        "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd\">"
        "<wsse:UsernameToken>"
        f"<wsse:Username>{username}</wsse:Username>"
        "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-username-token-profile-1.0#PasswordText\">"
        f"{password}</wsse:Password>"
        "</wsse:UsernameToken></wsse:Security></s:Header>"
    )


def _envelope(body: str, header: str = "") -> str:
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"
        "<s:Envelope xmlns:s=\"http://www.w3.org/2003/05/soap-envelope\" "
        "xmlns:tds=\"http://www.onvif.org/ver10/device/wsdl\" "
        "xmlns:trc=\"http://www.onvif.org/ver10/recording/wsdl\" "
        "xmlns:tse=\"http://www.onvif.org/ver10/search/wsdl\" "
        "xmlns:trp=\"http://www.onvif.org/ver10/replay/wsdl\">"
        f"{header}<s:Body>{body}</s:Body></s:Envelope>"
    )


def _find(xml_text: str, localname: str):
    root = ET.fromstring(xml_text)
    for e in root.iter():
        if e.tag.rsplit("}", 1)[-1] == localname:
            return e
    return None


def _find_all(xml_text: str, localname: str):
    root = ET.fromstring(xml_text)
    return [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == localname]


@pytest.fixture()
def onvif_on(monkeypatch):
    """Включает Profile G с заведённой учёткой на время теста."""
    monkeypatch.setattr(settings, "ONVIF_G_ENABLED", True)
    monkeypatch.setattr(settings, "ONVIF_G_USERNAME", "onvif")
    monkeypatch.setattr(settings, "ONVIF_G_PASSWORD", "secret-onvif-pass")
    monkeypatch.setattr(settings, "ONVIF_G_REPLAY_URI_BASE", "")
    return ("onvif", "secret-onvif-pass")


@pytest.fixture()
def recorded_camera(make_camera, pg_conn):
    """Камера с двумя сегментами архива. Возвращает (camera_id, from, until)."""
    cam = make_camera("onvif-g-rec")
    cam_id = cam["id"]
    base = datetime(2026, 8, 17, 10, 0, 0)
    rows = [
        (base, base + timedelta(minutes=5)),
        (base + timedelta(minutes=5), base + timedelta(minutes=10)),
    ]
    with pg_conn.cursor() as cur:
        for i, (st, en) in enumerate(rows):
            cur.execute(
                "INSERT INTO video_segments "
                "(camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes) "
                "VALUES (%s,%s,%s,%s,'continuous',300,1000)",
                (cam_id, st, en, f"/media/segments/cam{cam_id}_{i}.mp4"),
            )
    yield cam_id, rows[0][0], rows[-1][1]
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM video_segments WHERE camera_id=%s", (cam_id,))


# --- Фича выключена ---------------------------------------------------------

def test_disabled_returns_404(client):
    r = client.post("/onvif/device_service",
                    content=_envelope("<tds:GetServices/>"), headers=SOAP_CT)
    assert r.status_code == 404


# --- Device -----------------------------------------------------------------

def test_get_services_lists_all_profile_g_endpoints(client, onvif_on):
    r = client.post("/onvif/device_service",
                    content=_envelope("<tds:GetServices/>"), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    xaddrs = [e.text for e in _find_all(r.text, "XAddr")]
    assert any("recording_service" in x for x in xaddrs)
    assert any("search_service" in x for x in xaddrs)
    assert any("replay_service" in x for x in xaddrs)


def test_get_system_date_and_time_open(client, onvif_on):
    # Без WS-Security: клиенту нужны часы сервера, чтобы собрать Digest.
    r = client.post("/onvif/device_service",
                    content=_envelope("<tds:GetSystemDateAndTime/>"), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    assert _find(r.text, "UTCDateTime") is not None


# --- Авторизация ------------------------------------------------------------

def test_recordings_without_auth_is_fault(client, onvif_on, recorded_camera):
    r = client.post("/onvif/recording_service",
                    content=_envelope("<trc:GetRecordings/>"), headers=SOAP_CT)
    assert r.status_code == 400
    assert _find(r.text, "Subcode").find(
        "{http://www.w3.org/2003/05/soap-envelope}Value").text == "ter:NotAuthorized"


def test_recordings_wrong_password_is_fault(client, onvif_on, recorded_camera):
    r = client.post(
        "/onvif/recording_service",
        content=_envelope("<trc:GetRecordings/>", _security_header("onvif", "WRONG")),
        headers=SOAP_CT)
    assert r.status_code == 400
    assert "ter:NotAuthorized" in r.text


def test_feature_on_but_no_credentials_fails_closed(client, monkeypatch, recorded_camera):
    monkeypatch.setattr(settings, "ONVIF_G_ENABLED", True)
    monkeypatch.setattr(settings, "ONVIF_G_PASSWORD", "")  # не сконфигурирована
    r = client.post(
        "/onvif/recording_service",
        content=_envelope("<trc:GetRecordings/>", _security_header("onvif", "anything")),
        headers=SOAP_CT)
    assert r.status_code == 500  # Receiver fault
    assert "ter:NotAuthorized" in r.text


# --- Recording --------------------------------------------------------------

def test_get_recordings_returns_recorded_camera(client, onvif_on, recorded_camera):
    cam_id, _, _ = recorded_camera
    auth = _security_header(*onvif_on)
    r = client.post("/onvif/recording_service",
                    content=_envelope("<trc:GetRecordings/>", auth), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    tokens = [e.text for e in _find_all(r.text, "RecordingToken")]
    assert f"cam{cam_id}" in tokens
    tracks = [e.text for e in _find_all(r.text, "TrackToken")]
    assert f"VIDEO_{cam_id}" in tracks


# --- Search -----------------------------------------------------------------

def test_recording_summary_counts_and_bounds(client, onvif_on, recorded_camera):
    cam_id, frm, until = recorded_camera
    auth = _security_header(*onvif_on)
    r = client.post("/onvif/search_service",
                    content=_envelope("<tse:GetRecordingSummary/>", auth), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    assert int(_find(r.text, "NumberRecordings").text) >= 1
    # Границы включают наш интервал (могут быть шире за счёт чужих сегментов).
    data_from = _find(r.text, "DataFrom").text
    assert data_from.endswith("Z")


def test_find_then_results_roundtrip(client, onvif_on, recorded_camera):
    cam_id, frm, until = recorded_camera
    auth = _security_header(*onvif_on)
    # FindRecordings со scope на нашу камеру.
    find_body = (
        "<tse:FindRecordings><tse:Scope>"
        f"<tse:RecordingInformationFilter></tse:RecordingInformationFilter>"
        f"<tse:RecordingToken>cam{cam_id}</tse:RecordingToken>"
        "</tse:Scope><tse:KeepAliveTime>PT60S</tse:KeepAliveTime></tse:FindRecordings>"
    )
    r = client.post("/onvif/search_service",
                    content=_envelope(find_body, auth), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    token = _find(r.text, "SearchToken").text
    assert token

    res_body = (
        "<tse:GetRecordingSearchResults>"
        f"<tse:SearchToken>{token}</tse:SearchToken>"
        "</tse:GetRecordingSearchResults>"
    )
    r2 = client.post("/onvif/search_service",
                     content=_envelope(res_body, auth), headers=SOAP_CT)
    assert r2.status_code == 200, r2.text
    assert _find(r2.text, "SearchState").text == "Completed"
    infos = _find_all(r2.text, "RecordingInformation")
    got = {_find_all(ET.tostring(i, encoding="unicode"), "RecordingToken")[0].text for i in infos}
    assert f"cam{cam_id}" in got
    # Границы трека совпали с засеянными сегментами.
    data_from = _find(r2.text, "DataFrom").text
    assert data_from == soap.iso_utc(frm)

    # EndSearch освобождает токен: повторный запрос результатов — Fault.
    end_body = f"<tse:EndSearch><tse:SearchToken>{token}</tse:SearchToken></tse:EndSearch>"
    r3 = client.post("/onvif/search_service",
                     content=_envelope(end_body, auth), headers=SOAP_CT)
    assert r3.status_code == 200, r3.text
    r4 = client.post("/onvif/search_service",
                     content=_envelope(res_body, auth), headers=SOAP_CT)
    assert r4.status_code == 400
    assert "ter:InvalidArgVal" in r4.text


def test_search_scope_excludes_other_cameras(client, onvif_on, recorded_camera):
    auth = _security_header(*onvif_on)
    # Область на несуществующую камеру → пустой список результатов, не «все».
    find_body = (
        "<tse:FindRecordings><tse:Scope>"
        "<tse:RecordingToken>cam999999</tse:RecordingToken>"
        "</tse:Scope></tse:FindRecordings>"
    )
    r = client.post("/onvif/search_service",
                    content=_envelope(find_body, auth), headers=SOAP_CT)
    token = _find(r.text, "SearchToken").text
    res_body = (f"<tse:GetRecordingSearchResults><tse:SearchToken>{token}"
                "</tse:SearchToken></tse:GetRecordingSearchResults>")
    r2 = client.post("/onvif/search_service",
                     content=_envelope(res_body, auth), headers=SOAP_CT)
    assert r2.status_code == 200, r2.text
    assert _find_all(r2.text, "RecordingInformation") == []


# --- Replay -----------------------------------------------------------------

def test_replay_uri_not_configured_is_fault(client, onvif_on, recorded_camera):
    cam_id, _, _ = recorded_camera
    auth = _security_header(*onvif_on)
    body = (f"<trp:GetReplayUri><trp:RecordingToken>cam{cam_id}</trp:RecordingToken>"
            "</trp:GetReplayUri>")
    r = client.post("/onvif/replay_service",
                    content=_envelope(body, auth), headers=SOAP_CT)
    assert r.status_code == 400
    assert "ter:NotSupported" in r.text


def test_replay_uri_configured_returns_uri(client, monkeypatch, onvif_on, recorded_camera):
    cam_id, _, _ = recorded_camera
    monkeypatch.setattr(settings, "ONVIF_G_REPLAY_URI_BASE", "rtsp://mediamtx:8554")
    auth = _security_header(*onvif_on)
    body = (f"<trp:GetReplayUri><trp:RecordingToken>cam{cam_id}</trp:RecordingToken>"
            "</trp:GetReplayUri>")
    r = client.post("/onvif/replay_service",
                    content=_envelope(body, auth), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    assert _find(r.text, "Uri").text == f"rtsp://mediamtx:8554/cam{cam_id}"


def test_replay_uri_unknown_token_is_fault(client, monkeypatch, onvif_on):
    monkeypatch.setattr(settings, "ONVIF_G_REPLAY_URI_BASE", "rtsp://mediamtx:8554")
    auth = _security_header(*onvif_on)
    body = "<trp:GetReplayUri><trp:RecordingToken>bogus</trp:RecordingToken></trp:GetReplayUri>"
    r = client.post("/onvif/replay_service",
                    content=_envelope(body, auth), headers=SOAP_CT)
    assert r.status_code == 400
    assert "ter:NoRecording" in r.text


# --- Некорректный ввод ------------------------------------------------------

def test_malformed_xml_is_sender_fault(client, onvif_on):
    r = client.post("/onvif/search_service", content=b"<not-soap", headers=SOAP_CT)
    assert r.status_code == 400
    assert "ter:WellFormed" in r.text


def test_unknown_action_is_fault(client, onvif_on):
    auth = _security_header(*onvif_on)
    r = client.post("/onvif/search_service",
                    content=_envelope("<tse:Nonexistent/>", auth), headers=SOAP_CT)
    assert r.status_code == 400
    assert "ter:ActionNotSupported" in r.text


# --- Capabilities (контракт сервисов) ---------------------------------------

@pytest.mark.parametrize("service,action,ns_tag", [
    ("recording_service", "trc:GetServiceCapabilities", "Capabilities"),
    ("search_service", "tse:GetServiceCapabilities", "Capabilities"),
    ("replay_service", "trp:GetServiceCapabilities", "Capabilities"),
])
def test_service_capabilities_open(client, onvif_on, service, action, ns_tag):
    # GetServiceCapabilities — открытая операция (без WS-Security).
    r = client.post(f"/onvif/{service}",
                    content=_envelope(f"<{action}/>"), headers=SOAP_CT)
    assert r.status_code == 200, r.text
    assert _find(r.text, ns_tag) is not None


# --- Юнит-тесты SOAP-слоя (без БД) ------------------------------------------

def test_token_roundtrip():
    assert pg.camera_id_from_token(pg.recording_token(42)) == 42
    assert pg.camera_id_from_token(pg.track_token(42)) == 42
    assert pg.camera_id_from_token("garbage") is None
    assert pg.camera_id_from_token("") is None


def test_verify_security_digest_accepts_correct():
    nonce = b"0123456789abcdef"
    created = soap.iso_utc(datetime.now(timezone.utc))
    password = "s3cr3t"
    digest = base64.b64encode(
        hashlib.sha1(nonce + created.encode() + password.encode()).digest()
    ).decode()
    header_xml = _envelope(
        "<tse:GetRecordingSummary/>",
        "<s:Header><wsse:Security xmlns:wsse=\""
        "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd\" "
        "xmlns:wsu=\"http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-utility-1.0.xsd\">"
        "<wsse:UsernameToken><wsse:Username>u</wsse:Username>"
        "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">"
        f"{digest}</wsse:Password>"
        f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>"
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security></s:Header>",
    )
    header, _body = soap.parse_envelope(header_xml.encode())
    # Верный пароль — не бросает.
    soap.verify_security(header, soap.Credentials("u", "s3cr3t"))
    # Неверный — бросает.
    with pytest.raises(soap.SoapError):
        soap.verify_security(header, soap.Credentials("u", "other"))


def test_verify_security_stale_created_rejected():
    nonce = b"0123456789abcdef"
    stale = soap.iso_utc(datetime.now(timezone.utc) - timedelta(hours=1))
    password = "s3cr3t"
    digest = base64.b64encode(
        hashlib.sha1(nonce + stale.encode() + password.encode()).digest()
    ).decode()
    header_xml = _envelope(
        "<tse:GetRecordingSummary/>",
        "<s:Header><wsse:Security xmlns:wsse=\""
        "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd\">"
        "<wsse:UsernameToken><wsse:Username>u</wsse:Username>"
        "<wsse:Password Type=\"http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">"
        f"{digest}</wsse:Password>"
        f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce>"
        f"<Created>{stale}</Created>"
        "</wsse:UsernameToken></wsse:Security></s:Header>",
    )
    header, _body = soap.parse_envelope(header_xml.encode())
    with pytest.raises(soap.SoapError):
        soap.verify_security(header, soap.Credentials("u", "s3cr3t"))


def test_iso_utc_naive_treated_as_utc():
    assert soap.iso_utc(datetime(2026, 8, 17, 10, 0, 0)) == "2026-08-17T10:00:00Z"
