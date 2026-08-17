"""ONVIF Profile G (SPEC §12 «ONVIF … Profile G (хранение)») — сервер поиска
и воспроизведения записей для внешних VMS.

Четыре SOAP-эндпоинта (device/recording/search/replay), каждый принимает
конверт `POST`-ом и диспетчеризует по имени операции в Body. Разбор,
WS-Security и сборка ответа — в `services/onvif_soap.py`; данные о записях —
в `services/onvif_profile_g.py`.

**Что реализовано целиком** (метаданные архива, проверяемо без камеры):
GetServices/GetSystemDateAndTime (device), GetRecordings (recording),
GetRecordingSummary/FindRecordings/GetRecordingSearchResults/EndSearch
(search). Это «поисковая» половина Profile G — она отвечает на вопрос «какие
записи есть и в каких границах», и это ровно то, что FaceWatch знает точно.

**Что зависит от развёртывания:** GetReplayUri отдаёт RTSP-адрес из
`ONVIF_G_REPLAY_URI_BASE`; без него — ter:NotSupported. MediaMTX не отдаёт
ONVIF-replay по времени сам, поэтому боевой replay-источник настраивает
оператор (см. DEPLOY_CHECKLIST). Interop с конкретным VMS проверяется на
сервере — здесь проверяется контракт SOAP (форма запроса/ответа).

Фича выключена по умолчанию (`ONVIF_G_ENABLED`): при выключенной — 404
(эндпоинтов как будто нет), при включённой без пароля — отказ (fail-closed).
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..db import SessionLocal
from ..services import onvif_soap as soap
from ..services import onvif_profile_g as pg
from ..services.pubsub import get_redis

router = APIRouter(prefix="/onvif", tags=["onvif"])

# Версия, объявляемая в GetServices (ONVIF Core ~2.6).
_VER_MAJOR, _VER_MINOR = 2, 60

# Границы KeepAliveTime поисковой сессии (ISO 8601 PT..S).
_SEARCH_TTL_MIN, _SEARCH_TTL_MAX, _SEARCH_TTL_DEFAULT = 5, 600, 60

_SOAP_MEDIA = "application/soap+xml; charset=utf-8"


def _enabled() -> bool:
    return bool(settings.ONVIF_G_ENABLED)


def _credentials() -> soap.Credentials | None:
    """Настроенная ONVIF-учётка либо None, если пароль пуст (fail-closed)."""
    if not settings.ONVIF_G_PASSWORD:
        return None
    return soap.Credentials(username=settings.ONVIF_G_USERNAME,
                            password=settings.ONVIF_G_PASSWORD)


def _require_auth(header) -> None:
    creds = _credentials()
    if creds is None:
        # Фича включена, но учётка не заведена: не пускаем никого.
        raise soap.SoapError(
            "ter:NotAuthorized",
            "ONVIF Profile G включён, но учётная запись не сконфигурирована",
            receiver=True)
    soap.verify_security(header, creds)


def _reply(xml: str, status: int = 200) -> Response:
    return Response(content=xml, media_type=_SOAP_MEDIA, status_code=status)


def _fault_reply(err: soap.SoapError) -> Response:
    return _reply(soap.fault(err), status=err.http_status)


def _service_base(request: Request) -> str:
    """Базовый URL для XAddr в GetServices. За nginx это может быть
    внутренний адрес — то, что проверяют на сервере; здесь берём Host, как
    его видит приложение."""
    return str(request.base_url).rstrip("/")


def _iso_duration(seconds: int) -> str:
    return f"PT{int(seconds)}S"


def _parse_keepalive(body) -> int:
    raw = soap.text_of(body, "KeepAliveTime") or ""
    # PT60S → 60; при неразборчивом значении — дефолт.
    digits = "".join(ch for ch in raw if ch.isdigit())
    val = int(digits) if digits else _SEARCH_TTL_DEFAULT
    return max(_SEARCH_TTL_MIN, min(_SEARCH_TTL_MAX, val))


# --- Device service ---------------------------------------------------------

def _device_get_services(request: Request, body) -> str:
    base = _service_base(request)
    services = [
        ("tds", "http://www.onvif.org/ver10/device/wsdl", "device_service"),
        ("trc", "http://www.onvif.org/ver10/recording/wsdl", "recording_service"),
        ("tse", "http://www.onvif.org/ver10/search/wsdl", "search_service"),
        ("trp", "http://www.onvif.org/ver10/replay/wsdl", "replay_service"),
    ]
    items = "".join(
        "<tds:Service>"
        f"<tds:Namespace>{ns}</tds:Namespace>"
        f"<tds:XAddr>{soap.xml_escape(base + '/onvif/' + path)}</tds:XAddr>"
        f"<tds:Version><tt:Major>{_VER_MAJOR}</tt:Major>"
        f"<tt:Minor>{_VER_MINOR}</tt:Minor></tds:Version>"
        "</tds:Service>"
        for _p, ns, path in services
    )
    return soap.envelope(f"<tds:GetServicesResponse>{items}</tds:GetServicesResponse>")


def _device_get_system_date_and_time(body) -> str:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    return soap.envelope(
        "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
        "<tt:DateTimeType>Manual</tt:DateTimeType>"
        "<tt:DaylightSavings>false</tt:DaylightSavings>"
        "<tt:UTCDateTime>"
        f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute>"
        f"<tt:Second>{now.second}</tt:Second></tt:Time>"
        f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month>"
        f"<tt:Day>{now.day}</tt:Day></tt:Date>"
        "</tt:UTCDateTime>"
        "</tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
    )


# --- Recording service ------------------------------------------------------

async def _recording_get_recordings(db: AsyncSession) -> str:
    recs = await pg.list_recordings(db)
    items = "".join(
        "<trc:RecordingItem>"
        f"<trc:RecordingToken>{soap.xml_escape(r.token)}</trc:RecordingToken>"
        "<trc:Configuration>"
        "<tt:Source>"
        f"<tt:SourceId>{soap.xml_escape(r.token)}</tt:SourceId>"
        f"<tt:Name>{soap.xml_escape(r.name)}</tt:Name>"
        f"<tt:Location>{soap.xml_escape(r.location)}</tt:Location>"
        f"<tt:Description>FaceWatch camera {r.camera_id}</tt:Description>"
        f"<tt:Address>{soap.xml_escape(r.token)}</tt:Address>"
        "</tt:Source>"
        "<tt:Content>FaceWatch continuous recording</tt:Content>"
        "<tt:MaximumRetentionTime>PT0S</tt:MaximumRetentionTime>"
        "</trc:Configuration>"
        "<trc:Tracks><trc:Track>"
        f"<trc:TrackToken>{soap.xml_escape(r.track)}</trc:TrackToken>"
        "<trc:Configuration>"
        "<tt:TrackType>Video</tt:TrackType>"
        f"<tt:Description>Video track of {soap.xml_escape(r.name)}</tt:Description>"
        "</trc:Configuration>"
        "</trc:Track></trc:Tracks>"
        "</trc:RecordingItem>"
        for r in recs
    )
    return soap.envelope(f"<trc:GetRecordingsResponse>{items}</trc:GetRecordingsResponse>")


# --- Search service ---------------------------------------------------------

async def _search_get_summary(db: AsyncSession) -> str:
    s = await pg.get_summary(db)
    inner = "<tse:Summary>"
    if s.data_from is not None:
        inner += f"<tt:DataFrom>{soap.iso_utc(s.data_from)}</tt:DataFrom>"
        inner += f"<tt:DataUntil>{soap.iso_utc(s.data_until)}</tt:DataUntil>"
    inner += f"<tt:NumberRecordings>{s.number_recordings}</tt:NumberRecordings>"
    inner += "</tse:Summary>"
    return soap.envelope(f"<tse:GetRecordingSummaryResponse>{inner}</tse:GetRecordingSummaryResponse>")


async def _search_find_recordings(body) -> str:
    scope_el = soap.find_local(body, "Scope")
    recs, srcs = [], []
    if scope_el is not None:
        recs = [n.text.strip() for n in soap.find_all_local(scope_el, "RecordingToken")
                if n.text]
        srcs = [n.text.strip() for n in soap.find_all_local(scope_el, "SourceToken")
                if n.text]
    scope = pg.parse_scope(recs, srcs)
    ttl = _parse_keepalive(body)

    import secrets
    token = "FaceWatchSearch_" + secrets.token_hex(12)
    await get_redis().set(f"onvif:g:search:{token}",
                          json.dumps(scope.as_dict()), ex=ttl)
    return soap.envelope(
        f"<tse:FindRecordingsResponse><tse:SearchToken>{token}"
        "</tse:SearchToken></tse:FindRecordingsResponse>")


async def _search_get_results(db: AsyncSession, body) -> str:
    token = soap.text_of(body, "SearchToken")
    if not token:
        raise soap.SoapError("ter:InvalidArgVal", "SearchToken обязателен")
    raw = await get_redis().get(f"onvif:g:search:{token}")
    if raw is None:
        # Токен неизвестен или истёк по KeepAliveTime.
        raise soap.SoapError("ter:InvalidArgVal", "Неизвестный или истёкший SearchToken")
    scope = pg.SearchScope.from_dict(json.loads(raw))

    max_results = soap.text_of(body, "MaxResults")
    limit = int(max_results) if (max_results and max_results.isdigit()) else None

    recs = await pg.list_recordings(db, camera_ids=scope.included)
    if limit is not None:
        recs = recs[:limit]

    infos = "".join(
        "<tt:RecordingInformation>"
        f"<tt:RecordingToken>{soap.xml_escape(r.token)}</tt:RecordingToken>"
        "<tt:Source>"
        f"<tt:SourceId>{soap.xml_escape(r.token)}</tt:SourceId>"
        f"<tt:Name>{soap.xml_escape(r.name)}</tt:Name>"
        f"<tt:Location>{soap.xml_escape(r.location)}</tt:Location>"
        f"<tt:Description>FaceWatch camera {r.camera_id}</tt:Description>"
        f"<tt:Address>{soap.xml_escape(r.token)}</tt:Address>"
        "</tt:Source>"
        f"<tt:EarliestRecording>{soap.iso_utc(r.earliest)}</tt:EarliestRecording>"
        f"<tt:LatestRecording>{soap.iso_utc(r.latest)}</tt:LatestRecording>"
        "<tt:Content>FaceWatch continuous recording</tt:Content>"
        "<tt:Track>"
        f"<tt:TrackToken>{soap.xml_escape(r.track)}</tt:TrackToken>"
        "<tt:TrackType>Video</tt:TrackType>"
        f"<tt:Description>Video track of {soap.xml_escape(r.name)}</tt:Description>"
        f"<tt:DataFrom>{soap.iso_utc(r.earliest)}</tt:DataFrom>"
        f"<tt:DataTo>{soap.iso_utc(r.latest)}</tt:DataTo>"
        "</tt:Track>"
        "<tt:RecordingStatus>Stopped</tt:RecordingStatus>"
        "</tt:RecordingInformation>"
        for r in recs
    )
    # Поиск в FaceWatch синхронный (запрос-агрегат в БД), поэтому состояние
    # всегда Completed: очереди/долгого прохода, ради которых ONVIF вводит
    # опрос результатов, здесь нет.
    return soap.envelope(
        "<tse:GetRecordingSearchResultsResponse><tse:ResultList>"
        "<tt:SearchState>Completed</tt:SearchState>"
        f"{infos}"
        "</tse:ResultList></tse:GetRecordingSearchResultsResponse>")


async def _search_end_search(body) -> str:
    token = soap.text_of(body, "SearchToken")
    if token:
        await get_redis().delete(f"onvif:g:search:{token}")
    return soap.envelope("<tse:EndSearchResponse/>")


# --- Replay service ---------------------------------------------------------

def _replay_get_replay_uri(body) -> str:
    token = soap.text_of(body, "RecordingToken")
    cam_id = pg.camera_id_from_token(token or "")
    if cam_id is None:
        raise soap.SoapError("ter:NoRecording", "Неизвестный RecordingToken")
    base = settings.ONVIF_G_REPLAY_URI_BASE.strip().rstrip("/")
    if not base:
        # Честно: без настроенного replay-источника ссылку выдать нечем.
        raise soap.SoapError(
            "ter:NotSupported",
            "Replay-источник не сконфигурирован (ONVIF_G_REPLAY_URI_BASE)")
    uri = f"{base}/{pg.recording_token(cam_id)}"
    return soap.envelope(
        f"<trp:GetReplayUriResponse><trp:Uri>{soap.xml_escape(uri)}</trp:Uri>"
        "</trp:GetReplayUriResponse>")


# --- Диспетчеры эндпоинтов --------------------------------------------------

async def _dispatch(request: Request, service: str) -> Response:
    if not _enabled():
        # Фича выключена: эндпоинтов как будто нет вовсе.
        return _reply("Not found", status=404)
    raw = await request.body()
    try:
        header, body = soap.parse_envelope(raw)
        action = soap.body_action(body)
        if action is None:
            raise soap.SoapError("ter:WellFormed", "Пустое тело запроса")

        # GetSystemDateAndTime и GetServices — без авторизации (клиенту нужно
        # синхронизировать часы под Digest ещё до предъявления учётки, а
        # список сервисов — низкочувствительные адреса эндпоинтов). Всё
        # остальное — под WS-Security.
        open_actions = {"GetSystemDateAndTime", "GetServices", "GetServiceCapabilities"}
        if action not in open_actions:
            _require_auth(header)

        if service == "device":
            if action == "GetServices":
                return _reply(_device_get_services(request, body))
            if action == "GetSystemDateAndTime":
                return _reply(_device_get_system_date_and_time(body))
            if action == "GetServiceCapabilities":
                return _reply(soap.envelope(
                    "<tds:GetServiceCapabilitiesResponse/>"))
        elif service == "recording":
            async with SessionLocal() as db:
                if action == "GetRecordings":
                    return _reply(await _recording_get_recordings(db))
                if action == "GetServiceCapabilities":
                    return _reply(soap.envelope(
                        '<trc:GetServiceCapabilitiesResponse><trc:Capabilities '
                        'DynamicRecordings="false" DynamicTracks="false" '
                        'Encoding="H264 H265" MaxRecordings="0"/>'
                        '</trc:GetServiceCapabilitiesResponse>'))
        elif service == "search":
            async with SessionLocal() as db:
                if action == "GetRecordingSummary":
                    return _reply(await _search_get_summary(db))
                if action == "FindRecordings":
                    return _reply(await _search_find_recordings(body))
                if action == "GetRecordingSearchResults":
                    return _reply(await _search_get_results(db, body))
                if action == "EndSearch":
                    return _reply(await _search_end_search(body))
                if action == "GetServiceCapabilities":
                    return _reply(soap.envelope(
                        '<tse:GetServiceCapabilitiesResponse><tse:Capabilities '
                        'MetadataSearch="false" GeneralStartEvents="false"/>'
                        '</tse:GetServiceCapabilitiesResponse>'))
        elif service == "replay":
            if action == "GetReplayUri":
                return _reply(_replay_get_replay_uri(body))
            if action == "GetServiceCapabilities":
                return _reply(soap.envelope(
                    '<trp:GetServiceCapabilitiesResponse><trp:Capabilities '
                    'ReversePlayback="false" RTP_RTSP_TCP="true"/>'
                    '</trp:GetServiceCapabilitiesResponse>'))

        raise soap.SoapError("ter:ActionNotSupported",
                             f"Операция {action} не поддерживается сервисом {service}")
    except soap.SoapError as err:
        return _fault_reply(err)


@router.post("/device_service")
async def device_service(request: Request):
    return await _dispatch(request, "device")


@router.post("/recording_service")
async def recording_service(request: Request):
    return await _dispatch(request, "recording")


@router.post("/search_service")
async def search_service(request: Request):
    return await _dispatch(request, "search")


@router.post("/replay_service")
async def replay_service(request: Request):
    return await _dispatch(request, "replay")
