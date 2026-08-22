"""RTSP-сервер воспроизведения архива для ONVIF Profile G (SPEC §12).

**Зачем отдельный сервер, а не MediaMTX.** Слой записи (SPEC §2) — это
MediaMTX, и он же раздаёт live. Но Profile G требует от источника не
«отдай поток», а «отдай запись **с указанного момента настенного
времени**»: клиент присылает `PLAY` с `Range: clock=20260819T041000Z-`,
и сервер обязан начать с этой секунды архива. MediaMTX такого не умеет
вовсе — у него путь либо тянет камеру, либо принимает публикацию, а
записанные файлы он только пишет. Поэтому `GetReplayUri` до этого цикла
отдавал адрес, который оператор должен был поднять сам, а при пустой
настройке — честный `ter:NotSupported`. Воспроизведения не было.

**Что здесь есть.** Минимальный RTSP 1.0 сервер (RFC 2326) с тем
подмножеством, которое описывает ONVIF Streaming Specification для
replay:

* `Range: clock=` — вход по абсолютному времени, а не по смещению;
* `Rate-Control: no` — отдача быстрее реального времени (выгрузка);
* `Scale:` — ускоренное воспроизведение вперёд;
* `Require: onvif-replay` — клиент проверяет, что сервер понимает всё
  перечисленное, и обязан получить `551` со списком непонятого, если нет;
* RTP-расширение с абсолютным временем каждого кадра (заголовок `0xABAC`),
  без которого шкала VMS не знает, какому времени соответствует картинка.

**Откуда берётся видео.** Из тех же файлов сегментов, что отдаёт экспорт
(§5): `export.plan_pieces` раскладывает запрошенное окно на куски
сегментов со смещениями, а дальше на каждый кусок поднимается `ffmpeg
-c copy -f rtp` — remux без перекодирования, как того требует §24.

**Один ffmpeg на сегмент, а не один на всё окно.** Соблазн отдать
`concat` всех файлов разбивается о то же, обо что и в экспорте: склеенная
шкала непрерывна, настенная — нет. Но здесь цена ошибки выше, чем кривые
границы файла: в RTP-расширение пишется абсолютное время кадра, и после
первой же дыры в записи (реконнект RTSP, перезапуск слоя записи) VMS
показывал бы всю дальнейшую запись сдвинутой на длину дыры — то есть
уверенно врал бы о том, когда произошло событие. Поэтому время
привязывается к `started_at` **каждого** сегмента, и накопления ошибки
между сегментами не существует по построению.

Непрерывность потока для клиента при этом сохраняется: у всех кусков
принудительно один SSRC, сквозная нумерация RTP и сквозная шкала RTP —
переписываются здесь, в `rewrite_rtp_packet`, за один проход с
навешиванием расширения.

**Безопасность.** Точка внешняя и отдаёт архив, поэтому: выключена по
умолчанию вместе со всем Profile G; Digest-авторизация той же учёткой
`ONVIF_G_USERNAME`/`PASSWORD`, что и SOAP (RFC 2069/2617, как требует
ONVIF); пути файлов проверяются `export.within_media_root` — токен
записи разбирается в id камеры, а файлы берутся из БД, но проверка стоит
второй линией на случай испорченной строки индексации.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import shutil
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..config import settings
from ..db import SessionLocal
from ..models import VideoSegment
from . import export
from . import onvif_profile_g as pg

logger = logging.getLogger("facewatch.backend.rtsp_replay")

RTSP_VERSION = "RTSP/1.0"

# Возможности, которые клиент вправе потребовать через `Require`. Всё, чего
# здесь нет, обязано вернуться в `Unsupported` с кодом 551: ONVIF-клиент
# по этому ответу решает, можно ли вообще пользоваться источником, и молча
# проигнорированный `Require` означал бы «сервер сказал, что умеет».
SUPPORTED_FEATURES = {"onvif-replay"}

# Профиль RTP-расширения ONVIF (ONVIF Streaming Spec, «RTP header
# extension»): 12 байт полезной части — 8 байт NTP-времени кадра, байт
# флагов, байт CSeq, два зарезервированных.
ONVIF_EXT_PROFILE = 0xABAC
ONVIF_EXT_WORDS = 3

# Флаги расширения.
EXT_FLAG_C = 0x80  # clean point: кадр декодируется без предыдущих (I-frame)
EXT_FLAG_E = 0x40  # end: последний пакет непрерывного куска записи
EXT_FLAG_D = 0x20  # discontinuity: перед этим пакетом был разрыв записи

# Смещение от эпохи NTP (1900-01-01) до эпохи Unix.
NTP_EPOCH_DELTA = 2208988800

# Сколько ждать команды от клиента, прежде чем считать сессию брошенной.
# ONVIF отдаёт это значение в `GetReplayConfiguration.SessionTimeout`, и
# оно же уходит клиенту в `Session: <id>;timeout=<sec>`. Без таймаута
# оборванный TCP на стороне VMS оставлял бы жить ffmpeg — ровно та утечка
# процессов, которую §13 запрещает («graceful shutdown»).
SESSION_TIMEOUT_SEC = 60

# Потолок числа кусков, разбираемых за один заход в БД. Сутки записи одной
# камеры при 5-минутных сегментах — 288 строк; 512 берёт сутки с запасом и
# ограничивает размер выборки на архиве в сотни тысяч сегментов (§7).
PIECES_BATCH = 512

# Максимальный `Scale`. Ускорение делается чтением файла быстрее реального
# времени (`-readrate`), то есть упирается в диск: на 120 камерах запись
# идёт параллельно с выгрузкой, и неограниченный Scale от одного VMS
# отобрал бы полосу у слоя записи.
MAX_SCALE = 16.0

# Таймаут ожидания SDP от разогревочного запуска ffmpeg.
SDP_PROBE_TIMEOUT_SEC = 20


# --- Абсолютное время: разбор и печать --------------------------------------

_CLOCK_RE = re.compile(
    r"^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(?:\.(\d+))?Z$")


def parse_clock_time(value: str) -> datetime:
    """`20260819T041000Z` → naive-UTC datetime.

    Формат — `utc-time` из RFC 2326 (§3.7), а не ISO с дефисами: клиенты
    ONVIF присылают именно его. Возвращается naive-UTC, потому что в этом
    виде лежат `video_segments.started_at/ended_at` (TIMESTAMP WITHOUT TIME
    ZONE), и сравнение с aware-datetime было бы `TypeError` в обработчике,
    а не расхождением на пояс.
    """
    m = _CLOCK_RE.match(value.strip())
    if not m:
        raise ValueError(f"неразборчивое utc-time: {value!r}")
    year, mon, day, hour, minute, sec, frac = m.groups()
    micro = 0
    if frac:
        micro = int((frac + "000000")[:6])
    return datetime(int(year), int(mon), int(day), int(hour), int(minute),
                    int(sec), micro)


def format_clock_time(dt: datetime) -> str:
    """naive-UTC datetime → `20260819T041000Z`."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y%m%dT%H%M%SZ")


@dataclass(frozen=True)
class ClockRange:
    start: datetime
    end: datetime | None

    def __str__(self) -> str:
        tail = format_clock_time(self.end) if self.end else ""
        return f"clock={format_clock_time(self.start)}-{tail}"


def parse_clock_range(header: str) -> ClockRange:
    """`clock=20260819T041000Z-20260819T041500Z` → диапазон.

    Открытый правый край (`...Z-`) — «до конца записи», это обычная форма
    у VMS: оператор ткнул в точку на шкале и смотрит дальше. Обратный
    порядок границ (`Scale` < 0, обратное воспроизведение) отвергается
    вызывающим кодом — возможность объявлена выключенной в
    `GetServiceCapabilities` (`ReversePlayback="false"`).
    """
    raw = header.strip()
    if not raw.lower().startswith("clock="):
        raise ValueError("поддерживается только Range: clock=")
    body = raw[len("clock="):].strip()
    # Разделитель диапазона — дефис, а сами метки времени его не содержат
    # (utc-time пишется слитно), поэтому split по первому дефису однозначен.
    if "-" not in body:
        raise ValueError("в Range нет разделителя диапазона")
    left, right = body.split("-", 1)
    start = parse_clock_time(left)
    end = parse_clock_time(right) if right.strip() else None
    if end is not None and end <= start:
        raise ValueError("конец диапазона не позже начала")
    return ClockRange(start=start, end=end)


def parse_npt_seconds(header: str) -> float:
    """`npt=0.000-` → 0.0; `npt=12.5-30` → 12.5; `npt=now-` → 0.0.

    Зачем это здесь при наличии `clock=`. ONVIF-клиент присылает
    абсолютное время, но RTSP-клиент общего назначения (ffprobe, VLC,
    любая библиотека поверх libavformat) шлёт `npt` — смещение от начала
    потока, — и делает это в первом же `PLAY`. Отвечать ему `457 Invalid
    Range` значит объявить источник неисправным для всех, кроме VMS: ровно
    на этом ffprobe и споткнулся, когда replay проверили настоящим
    клиентом, а не только своим.

    `npt` трактуется как смещение от начала доступной записи — это и есть
    «начало потока» для источника, у которого поток и есть запись.
    """
    raw = header.strip()
    if not raw.lower().startswith("npt="):
        raise ValueError("не npt-диапазон")
    left = raw[4:].split("-", 1)[0].strip().lower()
    if not left or left == "now":
        return 0.0
    if ":" in left:
        # Форма `hh:mm:ss.frac` — RFC 2326 допускает и её.
        parts = [float(p) for p in left.split(":")]
        seconds = 0.0
        for part in parts:
            seconds = seconds * 60 + part
        return seconds
    return float(left)


def parse_range_header(header: str, available: ClockRange | None) -> ClockRange:
    """Разобрать `Range:` любой из двух форм в абсолютный диапазон.

    `available` нужен только для `npt`: смещение не значит ничего без
    точки, от которой отсчитывать.
    """
    lowered = header.strip().lower()
    if lowered.startswith("clock="):
        return parse_clock_range(header)
    if lowered.startswith("npt="):
        if available is None:
            raise ValueError("npt без известных границ записи")
        offset = parse_npt_seconds(header)
        start = available.start + timedelta(seconds=offset)
        if available.end is not None and start >= available.end:
            raise ValueError("смещение npt за концом записи")
        return ClockRange(start=start, end=available.end)
    raise ValueError(f"неподдерживаемая единица Range: {header!r}")


def ntp_timestamp(dt: datetime) -> int:
    """naive-UTC datetime → 64-битная метка NTP (32.32 fixed point)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    unix = dt.timestamp()
    seconds = int(unix) + NTP_EPOCH_DELTA
    frac = int((unix - int(unix)) * (1 << 32)) & 0xFFFFFFFF
    return ((seconds & 0xFFFFFFFF) << 32) | frac


def build_onvif_extension(when: datetime, *, clean_point: bool = False,
                          discontinuity: bool = False, end_of_section: bool = False,
                          cseq: int = 0) -> bytes:
    """RTP-расширение ONVIF с абсолютным временем кадра.

    Это единственное место, по которому VMS понимает, какому времени
    соответствует показанная картинка: RTP-шкала сама по себе относительна
    и после перемотки ничего не значит.
    """
    flags = 0
    if clean_point:
        flags |= EXT_FLAG_C
    if end_of_section:
        flags |= EXT_FLAG_E
    if discontinuity:
        flags |= EXT_FLAG_D
    return struct.pack(
        "!HHQBBH", ONVIF_EXT_PROFILE, ONVIF_EXT_WORDS, ntp_timestamp(when),
        flags, cseq & 0xFF, 0)


class RtpParseError(ValueError):
    """Пакет не разбирается как RTP — его нельзя переписать и нельзя слать."""


def rewrite_rtp_packet(packet: bytes, *, seq: int, timestamp: int, ssrc: int,
                       extension: bytes | None) -> bytes:
    """Переписать заголовок RTP и навесить расширение.

    Куски играются разными процессами ffmpeg, и без переписывания клиент
    видел бы на каждом сегменте новый поток: своя нумерация, своя шкала
    времени, свой SSRC. Здесь всем кускам навязывается сквозная нумерация,
    сквозная шкала и один идентификатор источника, а заодно вставляется
    расширение ONVIF — за тот же единственный проход по пакету.
    """
    if len(packet) < 12:
        raise RtpParseError("пакет короче заголовка RTP")
    b0 = packet[0]
    version = b0 >> 6
    if version != 2:
        raise RtpParseError(f"неожиданная версия RTP: {version}")
    csrc_count = b0 & 0x0F
    has_ext = bool(b0 & 0x10)
    head_len = 12 + 4 * csrc_count
    if len(packet) < head_len:
        raise RtpParseError("пакет короче заявленного числа CSRC")
    if has_ext:
        # Расширение от ffmpeg не приходит, но если бы пришло — своё
        # поверх чужого дало бы два расширения в одном пакете, что
        # запрещено RFC 3550. Снимаем чужое.
        if len(packet) < head_len + 4:
            raise RtpParseError("объявлено расширение, а его нет")
        ext_words = struct.unpack("!H", packet[head_len + 2:head_len + 4])[0]
        head_len += 4 + 4 * ext_words
        if len(packet) < head_len:
            raise RtpParseError("расширение выходит за границу пакета")
    payload = packet[head_len:]

    marker = packet[1] & 0x80
    payload_type = packet[1] & 0x7F
    b0_out = 0x80 | (0x10 if extension else 0)  # V=2, P=0, X, CC=0
    header = struct.pack("!BBHII", b0_out, marker | payload_type,
                         seq & 0xFFFF, timestamp & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
    return header + (extension or b"") + payload


def rtp_fields(packet: bytes) -> tuple[int, int, bool]:
    """(sequence, timestamp, marker) из пакета RTP."""
    if len(packet) < 12:
        raise RtpParseError("пакет короче заголовка RTP")
    seq, ts = struct.unpack("!HI", packet[2:8])
    return seq, ts, bool(packet[1] & 0x80)


# --- SDP --------------------------------------------------------------------

@dataclass
class MediaDescription:
    """Параметры дорожки, вынутые из SDP разогревочного запуска ffmpeg."""
    sdp_media_lines: list[str]
    payload_type: int
    clock_rate: int
    codec: str


def parse_ffmpeg_sdp(text: str) -> MediaDescription:
    """Вынуть из SDP, сгенерированного ffmpeg, описание единственной
    видеодорожки.

    Свой SDP собирается заново (`build_sdp`), а от ffmpeg берутся только
    строки, которые нельзя придумать: `m=`, `a=rtpmap`, `a=fmtp` с
    параметрами кодека (`sprop-parameter-sets` для H.264,
    `sprop-vps/sps/pps` для H.265). Без них клиент не соберёт декодер.
    """
    media: list[str] = []
    in_video = False
    payload_type = 96
    clock_rate = 90000
    codec = ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            if in_video:
                media.append(line)
                parts = line.split()
                if len(parts) >= 4 and parts[3].isdigit():
                    payload_type = int(parts[3])
            continue
        if not in_video:
            continue
        if line.startswith("a=rtpmap:"):
            media.append(line)
            m = re.match(r"a=rtpmap:\d+\s+([^/]+)/(\d+)", line)
            if m:
                codec = m.group(1)
                clock_rate = int(m.group(2))
        elif line.startswith("a=fmtp:"):
            media.append(line)
        elif line.startswith("b="):
            media.append(line)
    if not media:
        raise ValueError("в SDP от ffmpeg нет видеодорожки")
    return MediaDescription(sdp_media_lines=media, payload_type=payload_type,
                            clock_rate=clock_rate, codec=codec)


def build_sdp(desc: MediaDescription, *, session_name: str,
              available: ClockRange | None) -> str:
    """Собрать SDP ответа на DESCRIBE.

    `a=range:clock=...` на уровне сессии — то, по чему VMS рисует, за какие
    сутки у этой камеры вообще есть запись, ещё до первого PLAY.
    `a=control:trackID=0` нужен, чтобы SETUP пришёл на предсказуемый URL.
    """
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 0.0.0.0",
        f"s={session_name}",
        "c=IN IP4 0.0.0.0",
        "t=0 0",
        "a=control:*",
        "a=recvonly",
    ]
    if available is not None:
        lines.append(f"a=range:{available}")
    lines.extend(desc.sdp_media_lines)
    lines.append("a=control:trackID=0")
    return "\r\n".join(lines) + "\r\n"


# --- Digest-авторизация -----------------------------------------------------

class _Nonces:
    """Выданные nonce с временем выдачи.

    Свой кэш, а не тот, что у SOAP в Redis: там помнится nonce **клиента**
    (защита от повтора перехваченного UsernameToken), здесь — выданный
    **сервером**, и живёт он ровно между 401 и следующим запросом одного
    TCP-соединения.
    """

    def __init__(self, ttl_sec: int = 300) -> None:
        self._ttl = ttl_sec
        self._issued: dict[str, float] = {}

    def issue(self) -> str:
        self._sweep()
        nonce = secrets.token_hex(16)
        self._issued[nonce] = time.monotonic()
        return nonce

    def known(self, nonce: str) -> bool:
        self._sweep()
        return nonce in self._issued

    def _sweep(self) -> None:
        now = time.monotonic()
        for key, issued in list(self._issued.items()):
            if now - issued > self._ttl:
                self._issued.pop(key, None)


def digest_response(username: str, password: str, realm: str, nonce: str,
                    method: str, uri: str) -> str:
    """RFC 2069 (`qop` не используется — ONVIF-клиенты шлют базовую форму)."""
    ha1 = hashlib.md5(f"{username}:{realm}:{password}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    return hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()


def parse_digest_header(value: str) -> dict[str, str]:
    """`Digest username="x", nonce="y"` → словарь. Незакавыченные значения
    (их шлёт часть клиентов для `nc`/`qop`) разбираются наравне."""
    out: dict[str, str] = {}
    if not value.lower().startswith("digest "):
        return out
    for part in re.finditer(r'(\w+)\s*=\s*(?:"([^"]*)"|([^,\s]+))', value[7:]):
        out[part.group(1).lower()] = part.group(2) if part.group(2) is not None else part.group(3)
    return out


# --- План воспроизведения ---------------------------------------------------

@dataclass
class PlayPiece:
    """Кусок сегмента с привязкой к настенному времени."""
    path: str
    start_offset: float
    duration: float
    # Абсолютное время первого кадра куска (naive-UTC).
    started_at: datetime
    # Перед этим куском в записи была дыра (или это первый кусок после
    # перемотки) — в RTP-расширении поднимается флаг D.
    discontinuity: bool = False

    @property
    def ends_at(self) -> datetime:
        return self.started_at + timedelta(seconds=self.duration)


async def load_pieces(camera_id: int, start: datetime, end: datetime | None,
                      limit: int = PIECES_BATCH,
                      session_factory=None) -> list[PlayPiece]:
    """Куски записи камеры начиная с `start`, по возрастанию времени.

    Правый край `None` означает «до конца архива»: у VMS это обычная
    форма запроса. Дыры между кусками не заполняются — они помечаются
    флагом `discontinuity`, и решение, что с ними делать (ждать или
    прыгать), остаётся за сессией.

    `session_factory` подменяется только в тестах: сервер поднимается в
    том же event loop, что и приложение, и в эксплуатации берёт общий пул
    `SessionLocal`, а прогон из своего цикла обязан взять свой — пул
    asyncpg привязан к циклу, который его создал.
    """
    factory = session_factory or SessionLocal
    horizon = end
    async with factory() as db:
        q = (
            select(VideoSegment)
            .where(VideoSegment.camera_id == camera_id,
                   VideoSegment.ended_at > start)
            .order_by(VideoSegment.started_at)
            .limit(limit)
        )
        if horizon is not None:
            q = q.where(VideoSegment.started_at < horizon)
        segments = (await db.execute(q)).scalars().all()

    if not segments:
        return []
    if horizon is None:
        horizon = max(s.ended_at for s in segments)
    media_root = settings.MEDIA_PATH
    out: list[PlayPiece] = []
    prev_end: datetime | None = None
    for seg, piece in zip(segments, export.plan_pieces(segments, start, horizon)):
        if not export.within_media_root(piece.path, media_root):
            # Строка индексации, указывающая мимо медиа-каталога, — это
            # либо испорченная запись, либо попытка вычитать чужой файл
            # чужими руками. Пропускаем молча в поток, но громко в лог.
            logger.warning("сегмент вне MEDIA_PATH пропущен при replay",
                           extra={"path": piece.path, "camera_id": camera_id})
            continue
        if not os.path.isfile(piece.path):
            # Файл удалён ротацией retention (§5), а строка ещё есть.
            logger.info("файл сегмента отсутствует, пропущен при replay",
                        extra={"path": piece.path})
            continue
        piece_start = seg.started_at + timedelta(seconds=piece.start_offset)
        # Полсекунды допуска: MediaMTX закрывает файл и открывает следующий
        # не мгновенно, и без допуска флаг разрыва поднимался бы на каждой
        # штатной границе сегмента — то есть 288 «разрывов записи» в сутки
        # там, где запись не прерывалась.
        gap = prev_end is None or (piece_start - prev_end).total_seconds() > 0.5
        out.append(PlayPiece(path=piece.path, start_offset=piece.start_offset,
                             duration=piece.duration, started_at=piece_start,
                             discontinuity=bool(gap)))
        prev_end = piece_start + timedelta(seconds=piece.duration)
    return out


async def recording_range(camera_id: int,
                          session_factory=None) -> ClockRange | None:
    """Границы записи камеры — для `a=range:` в SDP."""
    factory = session_factory or SessionLocal
    async with factory() as db:
        recs = await pg.list_recordings(db, {camera_id})
    if not recs:
        return None
    r = recs[0]
    return ClockRange(start=r.earliest, end=r.latest)


# --- Источник пакетов: ffmpeg на кусок --------------------------------------

class _RtpReceiver(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue) -> None:
        self.queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            # Клиент не успевает читать. Молча терять кадр честнее, чем
            # копить память: RTP и так не гарантирует доставку, а рост
            # очереди на выгрузке в 16× съел бы память бэкенда.
            pass


def ffmpeg_piece_args(piece: PlayPiece, dest_port: int, *, payload_type: int,
                      rate: float | None) -> list[str]:
    """Аргументы ffmpeg для одного куска.

    `-c copy` — remux без перекодирования (§24). `-ss` до `-i` — вход по
    ближайшему ключевому кадру не позже запрошенного, та же логика, что и
    у экспорта: для видеонаблюдения лишние секунды до события лучше, чем
    потерянное начало.
    """
    args = ["-hide_banner", "-loglevel", "error", "-nostdin"]
    if rate is not None:
        # Реальный темп (или ускоренный по Scale). Без него ffmpeg
        # выгружает файл на скорости диска — это и есть `Rate-Control: no`.
        args += ["-readrate", f"{rate:.4f}"]
    if piece.start_offset > 0:
        args += ["-ss", f"{piece.start_offset:.3f}"]
    args += ["-i", piece.path, "-t", f"{piece.duration:.3f}"]
    args += ["-an", "-c:v", "copy", "-f", "rtp",
             "-payload_type", str(payload_type),
             # Свой SSRC и нумерацию ставим при переписывании пакета;
             # ffmpeg-овские нужны только чтобы не менялись внутри куска.
             "-rtpflags", "skip_rtcp",
             f"rtp://127.0.0.1:{dest_port}"]
    return args


async def probe_media_description(path: str) -> MediaDescription:
    """Описание дорожки файла сегмента: короткий прогон ffmpeg ради SDP.

    Придумать `sprop-parameter-sets` нельзя — они лежат в самом файле, и
    именно их клиент кладёт в декодер. Прогон идёт на 0.1 с в никуда,
    поэтому стоит доли секунды и не зависит от длины сегмента.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg не найден — воспроизведение невозможно")
    workdir = export.make_workdir()
    sdp_path = os.path.join(str(workdir), "probe.sdp")
    # Порт-приёмник открывается по-настоящему: ffmpeg на закрытый UDP-порт
    # получает ICMP unreachable и падает раньше, чем допишет SDP.
    sink = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sink.bind(("127.0.0.1", 0))
    port = sink.getsockname()[1]
    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin",
            "-i", path, "-t", "0.1", "-an", "-c:v", "copy", "-f", "rtp",
            "-payload_type", "96", "-sdp_file", sdp_path, "-y",
            f"rtp://127.0.0.1:{port}",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        try:
            _out, err = await asyncio.wait_for(proc.communicate(),
                                               timeout=SDP_PROBE_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError("ffmpeg не отдал SDP за отведённое время")
        if not os.path.isfile(sdp_path):
            tail = (err or b"").decode("utf-8", "replace")[-400:]
            raise RuntimeError(f"ffmpeg не создал SDP: {tail}")
        with open(sdp_path, "r", encoding="utf-8", errors="replace") as fh:
            return parse_ffmpeg_sdp(fh.read())
    finally:
        sink.close()
        export.cleanup_workdir(workdir)


# --- Сессия -----------------------------------------------------------------

@dataclass
class Transport:
    """Разобранный заголовок Transport клиента."""
    tcp: bool
    interleaved: tuple[int, int] = (0, 1)
    client_ports: tuple[int, int] | None = None
    raw: str = ""


def parse_transport(header: str) -> Transport:
    """Разбор `Transport:` — интерливинг по TCP и юникаст по UDP.

    ONVIF требует RTP/RTSP/TCP (это и объявлено в `GetServiceCapabilities`
    как `RTP_RTSP_TCP="true"`); UDP поддержан наравне, потому что внутри
    объекта VMS обычно просит именно его, а разница на нашей стороне —
    куда писать готовый пакет.
    """
    spec = header.split(",")[0].strip()
    lowered = spec.lower()
    if "/tcp" in lowered:
        m = re.search(r"interleaved=(\d+)-(\d+)", lowered)
        pair = (int(m.group(1)), int(m.group(2))) if m else (0, 1)
        return Transport(tcp=True, interleaved=pair, raw=spec)
    if "/udp" in lowered or "rtp/avp" in lowered:
        m = re.search(r"client_port=(\d+)-(\d+)", lowered)
        if not m:
            raise ValueError("в Transport нет client_port")
        return Transport(tcp=False, client_ports=(int(m.group(1)), int(m.group(2))),
                         raw=spec)
    raise ValueError(f"неподдерживаемый Transport: {spec}")


class ReplaySession:
    """Одна сессия воспроизведения: план, процесс ffmpeg и состояние RTP."""

    def __init__(self, session_id: str, camera_id: int) -> None:
        self.id = session_id
        self.camera_id = camera_id
        self.transport: Transport | None = None
        self.desc: MediaDescription | None = None
        self.ssrc = int.from_bytes(secrets.token_bytes(4), "big")
        self.seq = int.from_bytes(secrets.token_bytes(2), "big")
        self.rtp_base = int.from_bytes(secrets.token_bytes(4), "big") & 0x7FFFFFFF
        self.playing = False
        self.touched = time.monotonic()
        # Точка отсчёта шкалы RTP. Заводится от начала первого сыгранного
        # куска и держится на сессии: RTP-шкала обязана быть монотонной и
        # единой, иначе клиент перезапускал бы буфер на каждом сегменте.
        self.epoch: datetime | None = None
        self._task: asyncio.Task | None = None
        self._proc: asyncio.subprocess.Process | None = None

    def touch(self) -> None:
        self.touched = time.monotonic()

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.touched) > SESSION_TIMEOUT_SEC

    async def stop(self) -> None:
        """Снять воспроизведение и обязательно убить ffmpeg.

        Убивается именно здесь, а не в задаче: задача может быть уже
        отменена, и `terminate` в её `finally` не выполнится, если отмена
        пришла на `await` внутри. Оставленный ffmpeg — это открытый файл
        сегмента и процент ядра на каждую брошенную сессию.
        """
        self.playing = False
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._kill_proc()

    async def _kill_proc(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            proc.kill()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("ffmpeg replay не завершился после kill")


class RtspReplayServer:
    """Сам сервер: слушает TCP, ведёт сессии, отдаёт RTP.

    Живёт в процессе бэкенда, а не отдельным сервисом: он читает `video_
    segments` и `MEDIA_PATH`, то есть ровно то, что уже есть у приложения,
    и пятый systemd-юнит (§26) ради него заводить незачем. На слой записи
    он не влияет — читает закрытые файлы, MediaMTX не трогает вовсе, чем и
    соблюдается независимость слоёв (§2).
    """

    def __init__(self, host: str, port: int, *, username: str, password: str,
                 realm: str = "FaceWatch Replay", session_factory=None) -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.realm = realm
        self.session_factory = session_factory
        self._server: asyncio.AbstractServer | None = None
        self._nonces = _Nonces()
        self._sessions: dict[str, ReplaySession] = {}
        self._desc_cache: dict[int, MediaDescription] = {}
        self._reaper: asyncio.Task | None = None

    # --- жизненный цикл ---

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client,
                                                  self.host, self.port)
        self._reaper = asyncio.create_task(self._reap_loop())
        logger.info("RTSP replay сервер поднят",
                    extra={"host": self.host, "port": self.port})

    @property
    def bound_port(self) -> int:
        """Фактический порт (нужен тестам, которые просят порт 0)."""
        if self._server is None or not self._server.sockets:
            return self.port
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._reaper is not None:
            self._reaper.cancel()
            try:
                await self._reaper
            except asyncio.CancelledError:
                pass
            self._reaper = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for sess in list(self._sessions.values()):
            await sess.stop()
        self._sessions.clear()

    async def _reap_loop(self) -> None:
        """Убирает сессии, о которых клиент забыл.

        Брошенная сессия — это не только запись в словаре, но и живой
        ffmpeg; §13 «graceful shutdown» и утечки процессов разбираются
        именно здесь.
        """
        while True:
            await asyncio.sleep(5)
            for sid, sess in list(self._sessions.items()):
                if sess.expired:
                    logger.info("сессия replay снята по таймауту",
                                extra={"session": sid})
                    self._sessions.pop(sid, None)
                    await sess.stop()

    # --- разбор запроса ---

    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        owned: set[str] = set()
        try:
            while True:
                request = await self._read_request(reader)
                if request is None:
                    break
                method, uri, headers, _body = request
                response, session_id = await self._dispatch(
                    method, uri, headers, writer)
                if session_id:
                    owned.add(session_id)
                writer.write(response)
                await writer.drain()
                if method.upper() == "TEARDOWN":
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError):
            pass
        except Exception:  # noqa: BLE001
            logger.exception("ошибка в сессии RTSP replay", extra={"peer": str(peer)})
        finally:
            # Соединение закрылось — сессии этого соединения закрываются
            # вместе с ним. Иначе оборванный VMS оставлял бы играющий
            # ffmpeg до истечения таймаута сессии на каждой перезагрузке
            # своей страницы.
            for sid in owned:
                sess = self._sessions.pop(sid, None)
                if sess is not None:
                    await sess.stop()
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _read_request(self, reader: asyncio.StreamReader):
        """Прочитать один запрос RTSP. `None` — соединение закрыто.

        Интерливинговые пакеты клиента (`$`) пропускаются: RTCP RR от VMS
        приходит по тому же соединению, и без пропуска он был бы принят за
        начало запроса и порвал бы разбор.
        """
        while True:
            first = await reader.readexactly(1)
            if first != b"$":
                break
            head = await reader.readexactly(3)
            length = struct.unpack("!H", head[1:3])[0]
            await reader.readexactly(length)

        line = first + await reader.readline()
        if not line.strip():
            return None
        try:
            method, uri, _version = line.decode("utf-8", "replace").split()
        except ValueError:
            raise ValueError(f"неразборчивая строка запроса: {line!r}")
        headers: dict[str, str] = {}
        while True:
            raw = await reader.readline()
            if raw in (b"\r\n", b"\n", b""):
                break
            text = raw.decode("utf-8", "replace").strip()
            if ":" not in text:
                continue
            key, value = text.split(":", 1)
            headers[key.strip().lower()] = value.strip()
        body = b""
        length = int(headers.get("content-length", "0") or 0)
        if length:
            body = await reader.readexactly(length)
        return method, uri, headers, body

    # --- ответы ---

    def _response(self, code: int, reason: str, cseq: str,
                  headers: dict[str, str] | None = None, body: str = "") -> bytes:
        lines = [f"{RTSP_VERSION} {code} {reason}", f"CSeq: {cseq}",
                 "Server: FaceWatch/ONVIF-Replay"]
        for key, value in (headers or {}).items():
            lines.append(f"{key}: {value}")
        if body:
            lines.append(f"Content-Length: {len(body.encode())}")
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()

    def _unauthorized(self, cseq: str) -> bytes:
        nonce = self._nonces.issue()
        return self._response(401, "Unauthorized", cseq, {
            "WWW-Authenticate": (f'Digest realm="{self.realm}", nonce="{nonce}"'),
        })

    def _authorized(self, method: str, uri: str, headers: dict[str, str]) -> bool:
        """Проверка Digest.

        Пустой пароль — «фича включена, но не сконфигурирована»: сервер
        отказывает всем, а не пускает без пароля. Та же fail-closed логика,
        что у SOAP-половины Profile G.
        """
        if not self.password:
            return False
        parsed = parse_digest_header(headers.get("authorization", ""))
        if not parsed:
            return False
        if parsed.get("username") != self.username:
            return False
        nonce = parsed.get("nonce", "")
        if not nonce or not self._nonces.known(nonce):
            return False
        expected = digest_response(self.username, self.password, self.realm,
                                   nonce, method.upper(),
                                   parsed.get("uri", uri))
        return secrets.compare_digest(expected, parsed.get("response", ""))

    async def _dispatch(self, method: str, uri: str, headers: dict[str, str],
                        writer: asyncio.StreamWriter) -> tuple[bytes, str | None]:
        cseq = headers.get("cseq", "0")
        method = method.upper()

        # `Require` разбирается раньше всего: клиент, попросивший то, чего
        # мы не умеем, обязан узнать об этом до того, как получит поток,
        # который выглядит правильным и врёт про время.
        require = headers.get("require", "")
        unknown = [f.strip() for f in require.split(",")
                   if f.strip() and f.strip().lower() not in SUPPORTED_FEATURES]
        if unknown:
            return self._response(551, "Option not supported", cseq,
                                  {"Unsupported": ", ".join(unknown)}), None

        if method == "OPTIONS":
            return self._response(200, "OK", cseq, {
                "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN, GET_PARAMETER",
            }), None

        if not self._authorized(method, uri, headers):
            return self._unauthorized(cseq), None

        if method == "DESCRIBE":
            return await self._describe(uri, cseq)
        if method == "SETUP":
            return await self._setup(uri, cseq, headers)
        if method == "PLAY":
            return await self._play(uri, cseq, headers, writer)
        if method == "PAUSE":
            return await self._pause(cseq, headers)
        if method == "GET_PARAMETER":
            sess = self._sessions.get(headers.get("session", "").split(";")[0])
            if sess is not None:
                sess.touch()
            return self._response(200, "OK", cseq), None
        if method == "TEARDOWN":
            sid = headers.get("session", "").split(";")[0]
            sess = self._sessions.pop(sid, None)
            if sess is not None:
                await sess.stop()
            return self._response(200, "OK", cseq), None
        return self._response(405, "Method Not Allowed", cseq), None

    def _camera_from_uri(self, uri: str) -> int | None:
        """`rtsp://host:8555/cam7` или `.../cam7/trackID=0` → 7.

        Токен пути — тот же `cam{id}`, что у записи в Profile G и у пути
        MediaMTX: VMS получает его в `GetReplayUri` и не должен угадывать
        второе имя для того же самого.
        """
        path = uri.split("://", 1)[-1]
        path = path.split("/", 1)[1] if "/" in path else ""
        path = path.split("?", 1)[0].strip("/")
        for part in path.split("/"):
            cam = pg.camera_id_from_token(part)
            if cam is not None:
                return cam
        return None

    async def _description_for(self, camera_id: int) -> MediaDescription | None:
        """Описание дорожки камеры, с кэшем на процесс.

        Кэш по камере, а не по файлу: параметры кодека у одной камеры между
        сегментами не меняются (их задаёт сама камера), а разогревочный
        запуск ffmpeg на каждый DESCRIBE стоил бы VMS лишних полсекунды на
        каждое открытие плеера.
        """
        cached = self._desc_cache.get(camera_id)
        if cached is not None:
            return cached
        pieces = await load_pieces(camera_id, datetime(1970, 1, 1), None, limit=1,
                                   session_factory=self.session_factory)
        if not pieces:
            return None
        desc = await probe_media_description(pieces[0].path)
        self._desc_cache[camera_id] = desc
        return desc

    async def _describe(self, uri: str, cseq: str) -> tuple[bytes, str | None]:
        cam = self._camera_from_uri(uri)
        if cam is None:
            return self._response(404, "Not Found", cseq), None
        try:
            desc = await self._description_for(cam)
        except Exception:  # noqa: BLE001
            logger.exception("не удалось описать дорожку камеры",
                             extra={"camera_id": cam})
            return self._response(500, "Internal Server Error", cseq), None
        if desc is None:
            return self._response(404, "Not Found", cseq), None
        sdp = build_sdp(desc, session_name=pg.recording_token(cam),
                        available=await recording_range(
                            cam, session_factory=self.session_factory))
        return self._response(200, "OK", cseq, {
            "Content-Type": "application/sdp",
            "Content-Base": uri if uri.endswith("/") else uri + "/",
        }, sdp), None

    async def _setup(self, uri: str, cseq: str,
                     headers: dict[str, str]) -> tuple[bytes, str | None]:
        cam = self._camera_from_uri(uri)
        if cam is None:
            return self._response(404, "Not Found", cseq), None
        try:
            transport = parse_transport(headers.get("transport", ""))
        except ValueError:
            return self._response(461, "Unsupported Transport", cseq), None
        try:
            desc = await self._description_for(cam)
        except Exception:  # noqa: BLE001
            logger.exception("SETUP: не удалось описать дорожку",
                             extra={"camera_id": cam})
            return self._response(500, "Internal Server Error", cseq), None
        if desc is None:
            return self._response(404, "Not Found", cseq), None

        sid = secrets.token_hex(8)
        sess = ReplaySession(sid, cam)
        sess.transport = transport
        sess.desc = desc
        self._sessions[sid] = sess
        if transport.tcp:
            tr = (f"RTP/AVP/TCP;unicast;interleaved="
                  f"{transport.interleaved[0]}-{transport.interleaved[1]}")
        else:
            tr = (f"RTP/AVP;unicast;client_port="
                  f"{transport.client_ports[0]}-{transport.client_ports[1]};"
                  f"ssrc={sess.ssrc:08X}")
        return self._response(200, "OK", cseq, {
            "Transport": tr,
            "Session": f"{sid};timeout={SESSION_TIMEOUT_SEC}",
        }), sid

    async def _pause(self, cseq: str,
                     headers: dict[str, str]) -> tuple[bytes, str | None]:
        sid = headers.get("session", "").split(";")[0]
        sess = self._sessions.get(sid)
        if sess is None:
            return self._response(454, "Session Not Found", cseq), None
        sess.touch()
        await sess.stop()
        return self._response(200, "OK", cseq, {"Session": sid}), sid

    async def _play(self, uri: str, cseq: str, headers: dict[str, str],
                    writer: asyncio.StreamWriter) -> tuple[bytes, str | None]:
        sid = headers.get("session", "").split(";")[0]
        sess = self._sessions.get(sid)
        if sess is None:
            return self._response(454, "Session Not Found", cseq), None
        sess.touch()

        # Scale: ускоренное воспроизведение вперёд. Обратное объявлено
        # выключенным (`ReversePlayback="false"`), и отвечать на него надо
        # отказом, а не молча играть вперёд: VMS показал бы движение
        # задом наперёд, которого не было.
        try:
            scale = float(headers.get("scale", "1") or 1)
        except ValueError:
            scale = 1.0
        if scale < 0:
            return self._response(457, "Invalid Range", cseq), sid
        scale = min(max(scale, 0.1), MAX_SCALE)

        rate_control = headers.get("rate-control", "yes").strip().lower() != "no"
        rate = scale if rate_control else None

        # Границы записи нужны и для `npt` (смещение не от чего отсчитывать
        # без них), и для PLAY вовсе без Range.
        available = await recording_range(
            sess.camera_id, session_factory=self.session_factory)
        range_header = headers.get("range", "")
        if range_header:
            try:
                rng = parse_range_header(range_header, available)
            except ValueError:
                return self._response(457, "Invalid Range", cseq), sid
        else:
            # PLAY без Range после SETUP — «с начала того, что есть».
            if available is None:
                return self._response(404, "Not Found", cseq), sid
            rng = available

        pieces = await load_pieces(sess.camera_id, rng.start, rng.end,
                                   session_factory=self.session_factory)
        if not pieces:
            return self._response(404, "Not Found", cseq), sid

        await sess.stop()
        sess.playing = True
        sess.touch()
        actual = ClockRange(start=pieces[0].started_at, end=rng.end)
        sess._task = asyncio.create_task(self._stream(sess, pieces, rate, writer))

        resp_headers = {
            "Session": sid,
            "Range": str(actual),
            "Scale": f"{scale:g}",
            "RTP-Info": f"url={uri};seq={sess.seq};rtptime={sess.rtp_base}",
        }
        if not rate_control:
            resp_headers["Rate-Control"] = "no"
        return self._response(200, "OK", cseq, resp_headers), sid

    # --- отдача пакетов ---

    async def _stream(self, sess: ReplaySession, pieces: list[PlayPiece],
                      rate: float | None,
                      writer: asyncio.StreamWriter) -> None:
        """Проиграть план: по процессу ffmpeg на кусок, пакеты — клиенту."""
        try:
            for index, piece in enumerate(pieces):
                if not sess.playing:
                    return
                last = index == len(pieces) - 1
                await self._stream_piece(sess, piece, rate, writer,
                                         end_of_section=last)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("сбой воспроизведения архива",
                             extra={"camera_id": sess.camera_id})
        finally:
            sess.playing = False
            await sess._kill_proc()

    async def _stream_piece(self, sess: ReplaySession, piece: PlayPiece,
                            rate: float | None, writer: asyncio.StreamWriter,
                            *, end_of_section: bool) -> None:
        assert sess.desc is not None and sess.transport is not None
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("ffmpeg не найден — воспроизведение невозможно")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue(maxsize=4096)
        transport_udp, _proto = await loop.create_datagram_endpoint(
            lambda: _RtpReceiver(queue), local_addr=("127.0.0.1", 0))
        port = transport_udp.get_extra_info("socket").getsockname()[1]

        out_sock: socket.socket | None = None
        if not sess.transport.tcp:
            out_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        clock_rate = sess.desc.clock_rate
        if sess.epoch is None:
            sess.epoch = piece.started_at
        first_ts: int | None = None
        discontinuity = piece.discontinuity
        # Отдача с задержкой в один пакет. Флаг E («здесь запись
        # кончается») можно поставить только на пакет, про который уже
        # известно, что следующего не будет, — а узнать это, держа в руках
        # текущий, нельзя. Поэтому пакет уходит клиенту, когда пришёл
        # следующий, а последний — с флагом, после выхода из цикла.
        # Элемент очереди: (пакет, абсолютное время, ключевой кадр, разрыв).
        pending: tuple[bytes, datetime, bool, bool] | None = None

        async def flush(item, *, last: bool) -> None:
            raw, when, clean, disc = item
            ext = build_onvif_extension(
                when, clean_point=clean, discontinuity=disc,
                end_of_section=(last and end_of_section))
            out = rewrite_rtp_packet(
                raw, seq=sess.seq,
                timestamp=(sess.rtp_base + int(
                    (when - sess.epoch).total_seconds() * clock_rate)) & 0xFFFFFFFF,
                ssrc=sess.ssrc, extension=ext)
            sess.seq = (sess.seq + 1) & 0xFFFF
            sess.touch()
            if sess.transport.tcp:
                writer.write(b"$" + bytes([sess.transport.interleaved[0]])
                             + struct.pack("!H", len(out)) + out)
                await writer.drain()
            elif out_sock is not None:
                peer = writer.get_extra_info("peername")
                out_sock.sendto(out, (peer[0], sess.transport.client_ports[0]))

        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg, *ffmpeg_piece_args(piece, port,
                                           payload_type=sess.desc.payload_type,
                                           rate=rate),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE)
            sess._proc = proc
            while True:
                if not sess.playing:
                    return
                try:
                    packet = await asyncio.wait_for(queue.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    # Процесс закончил и очередь пуста — кусок доигран.
                    if proc.returncode is not None and queue.empty():
                        break
                    continue
                try:
                    _seq, ts, _marker = rtp_fields(packet)
                except RtpParseError:
                    continue
                if first_ts is None:
                    first_ts = ts
                # Смещение внутри куска в тактах RTP; арифметика по модулю
                # 2^32 переживает переполнение шкалы.
                delta = (ts - first_ts) & 0xFFFFFFFF
                if delta > 0x7FFFFFFF:
                    delta -= 0x100000000
                when = piece.started_at + timedelta(seconds=delta / clock_rate)
                # «С этого кадра можно начать декодирование» достоверно
                # известно только про первый пакет куска: `-ss` до `-i`
                # входит ровно по ключевому кадру.
                item = (packet, when, ts == first_ts, discontinuity)
                # Разрыв помечается только на первом пакете куска: дальше
                # запись идёт подряд.
                discontinuity = False
                if pending is not None:
                    await flush(pending, last=False)
                pending = item
            if pending is not None:
                await flush(pending, last=True)
            if proc.returncode not in (0, None):
                err = b""
                if proc.stderr is not None:
                    err = await proc.stderr.read()
                logger.warning("ffmpeg replay завершился с ошибкой",
                               extra={"code": proc.returncode,
                                      "stderr": err.decode("utf-8", "replace")[-300:]})
        finally:
            transport_udp.close()
            if out_sock is not None:
                out_sock.close()
            await sess._kill_proc()


# --- Точка входа приложения -------------------------------------------------

_server: RtspReplayServer | None = None


def replay_enabled() -> bool:
    """Встроенный replay включён вместе с Profile G, если оператор не задал
    внешний источник явно."""
    return bool(settings.ONVIF_G_ENABLED
                and settings.ONVIF_G_REPLAY_BUILTIN
                and not settings.ONVIF_G_REPLAY_URI_BASE.strip())


async def start_replay_server() -> RtspReplayServer | None:
    global _server
    if not replay_enabled():
        return None
    if not settings.ONVIF_G_PASSWORD:
        # Fail-closed, как и SOAP-половина: без пароля точка, раздающая
        # архив, не поднимается вовсе.
        logger.error("ONVIF replay не поднят: не задан ONVIF_G_PASSWORD")
        return None
    _server = RtspReplayServer(settings.ONVIF_G_REPLAY_HOST,
                               settings.ONVIF_G_REPLAY_PORT,
                               username=settings.ONVIF_G_USERNAME,
                               password=settings.ONVIF_G_PASSWORD)
    await _server.start()
    return _server


async def stop_replay_server() -> None:
    global _server
    if _server is not None:
        await _server.stop()
        _server = None


def replay_uri_base(host: str) -> str:
    """База RTSP-URI для GetReplayUri.

    Явно заданный `ONVIF_G_REPLAY_URI_BASE` имеет приоритет: оператор мог
    поставить перед FaceWatch свой прокси или пробросить порт наружу под
    другим адресом, и угадывать это за него нельзя.
    """
    explicit = settings.ONVIF_G_REPLAY_URI_BASE.strip().rstrip("/")
    if explicit:
        return explicit
    return f"rtsp://{host}:{settings.ONVIF_G_REPLAY_PORT}"
