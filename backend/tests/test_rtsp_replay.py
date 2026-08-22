"""ONVIF Profile G Replay (SPEC §12) — воспроизведение архива по RTSP.

Два уровня проверки.

**Разбор и сборка** (без ffmpeg и БД): формат `Range: clock=`, байты
RTP-расширения ONVIF, переписывание заголовка RTP, `Transport`, Digest.
Здесь ловятся ошибки в единственном месте, где мы говорим клиенту, какому
времени соответствует кадр.

**Настоящее воспроизведение** (ffmpeg + Postgres): на диск пишутся
настоящие сегменты MP4, строки заводятся в тот же Postgres, что у
приложения, поднимается настоящий сервер, и к нему приходит настоящий
RTSP-клиент — сырой, написанный здесь же, потому что ffmpeg как клиент
расширения RTP выбрасывает, а проверять надо именно их. Отдельным тестом
к тому же серверу приходит ffprobe: он проверяет то, чего сырой клиент не
проверяет, — что отданное действительно декодируется.

Почему клиент сырой, а не `python-rtsp`: проверяется наш протокольный
контракт с VMS, и клиент, который сам «починит» кривой ответ (подставит
дефолт, промолчит про отсутствующий заголовок), превратил бы тест в
проверку клиента.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services import rtsp_replay as rr


pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

HAVE_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


# --- Разбор абсолютного времени ---------------------------------------------

def test_parse_clock_time_utc_form():
    assert rr.parse_clock_time("20260819T041005Z") == datetime(2026, 8, 19, 4, 10, 5)


def test_parse_clock_time_with_fraction():
    assert rr.parse_clock_time("20260819T041005.25Z") == \
        datetime(2026, 8, 19, 4, 10, 5, 250000)


@pytest.mark.parametrize("bad", [
    "2026-08-19T04:10:05Z",  # ISO с дефисами — не utc-time RFC 2326
    "20260819T041005",       # без Z
    "npt=0-",
    "",
])
def test_parse_clock_time_rejects_other_forms(bad):
    with pytest.raises(ValueError):
        rr.parse_clock_time(bad)


def test_parse_clock_range_open_end():
    rng = rr.parse_clock_range("clock=20260819T041000Z-")
    assert rng.start == datetime(2026, 8, 19, 4, 10, 0)
    # Открытый правый край — «до конца записи», самая частая форма у VMS.
    assert rng.end is None


def test_parse_clock_range_closed():
    rng = rr.parse_clock_range("clock=20260819T041000Z-20260819T041500Z")
    assert rng.end == datetime(2026, 8, 19, 4, 15, 0)


def test_parse_clock_range_rejects_reversed():
    # Обратный порядок — это запрос обратного воспроизведения, а оно
    # объявлено выключенным (ReversePlayback="false").
    with pytest.raises(ValueError):
        rr.parse_clock_range("clock=20260819T041500Z-20260819T041000Z")


def test_parse_clock_range_rejects_npt():
    # npt — смещение от начала потока. Принять его молча значило бы
    # проиграть не то время, о котором просили, и не сказать об этом.
    with pytest.raises(ValueError):
        rr.parse_clock_range("npt=0-")


def test_clock_range_roundtrip_is_the_wire_form():
    rng = rr.ClockRange(datetime(2026, 8, 19, 4, 10), datetime(2026, 8, 19, 4, 15))
    assert str(rng) == "clock=20260819T041000Z-20260819T041500Z"
    assert rr.parse_clock_range(str(rng)) == rng


# --- RTP-расширение ONVIF ---------------------------------------------------

def test_ntp_timestamp_matches_known_value():
    # 1970-01-01T00:00:00Z — ровно граница эпох: старшее слово равно
    # смещению NTP, младшее нулю.
    assert rr.ntp_timestamp(datetime(1970, 1, 1)) == rr.NTP_EPOCH_DELTA << 32


def test_extension_layout_is_twelve_bytes_after_header():
    ext = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10))
    profile, words = struct.unpack("!HH", ext[:4])
    assert profile == 0xABAC
    assert words == 3
    # 4 байта заголовка расширения + 3 слова полезной части.
    assert len(ext) == 4 + 12


def test_extension_carries_the_frame_time():
    when = datetime(2026, 8, 19, 4, 10, 5, 500000)
    ext = rr.build_onvif_extension(when)
    ntp = struct.unpack("!Q", ext[4:12])[0]
    assert ntp == rr.ntp_timestamp(when)
    # Обратный пересчёт: секунды NTP минус смещение эпох — это Unix-время.
    seconds = ntp >> 32
    assert seconds - rr.NTP_EPOCH_DELTA == int(when.timestamp())


def test_extension_flags_are_independent():
    base = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10))
    assert base[12] == 0
    only_c = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10), clean_point=True)
    assert only_c[12] == rr.EXT_FLAG_C
    only_d = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10), discontinuity=True)
    assert only_d[12] == rr.EXT_FLAG_D
    both = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10),
                                    clean_point=True, end_of_section=True)
    assert both[12] == rr.EXT_FLAG_C | rr.EXT_FLAG_E


# --- Переписывание RTP ------------------------------------------------------

def _rtp(seq: int, ts: int, ssrc: int, *, marker: bool = False,
         payload: bytes = b"frame", pt: int = 96) -> bytes:
    return struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | pt,
                       seq, ts, ssrc) + payload


def test_rewrite_replaces_identity_and_keeps_payload():
    ext = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10))
    out = rr.rewrite_rtp_packet(_rtp(7, 1234, 0xAABBCCDD), seq=9, timestamp=555,
                                ssrc=0x11223344, extension=ext)
    seq, ts, marker = rr.rtp_fields(out)
    assert (seq, ts) == (9, 555)
    assert struct.unpack("!I", out[8:12])[0] == 0x11223344
    assert out.endswith(b"frame")
    assert marker is False


def test_rewrite_sets_extension_bit_and_inserts_extension():
    ext = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10))
    out = rr.rewrite_rtp_packet(_rtp(1, 2, 3), seq=1, timestamp=2, ssrc=3,
                                extension=ext)
    assert out[0] & 0x10, "бит X не поднят — клиент не станет читать расширение"
    assert out[12:12 + len(ext)] == ext


def test_rewrite_preserves_marker_and_payload_type():
    out = rr.rewrite_rtp_packet(_rtp(1, 2, 3, marker=True, pt=97), seq=1,
                                timestamp=2, ssrc=3, extension=None)
    assert out[1] & 0x80, "маркер конца кадра потерян"
    assert out[1] & 0x7F == 97


def test_rewrite_strips_foreign_extension():
    """Своё расширение поверх чужого дало бы два в одном пакете (запрещено
    RFC 3550), и клиент прочитал бы как ONVIF-время чужие байты."""
    foreign = struct.pack("!HH", 0xBEEF, 1) + b"\x00\x00\x00\x01"
    packet = (struct.pack("!BBHII", 0x90, 96, 5, 6, 7) + foreign + b"frame")
    ext = rr.build_onvif_extension(datetime(2026, 8, 19, 4, 10))
    out = rr.rewrite_rtp_packet(packet, seq=1, timestamp=2, ssrc=3, extension=ext)
    assert out.count(b"\xbe\xef") == 0
    assert out[12:12 + len(ext)] == ext
    assert out.endswith(b"frame")


def test_rewrite_rejects_non_rtp():
    with pytest.raises(rr.RtpParseError):
        rr.rewrite_rtp_packet(b"short", seq=1, timestamp=1, ssrc=1, extension=None)
    with pytest.raises(rr.RtpParseError):
        # Версия 1 вместо 2 — это не RTP, и слать такое клиенту нельзя.
        rr.rewrite_rtp_packet(bytes([0x40]) + b"\x00" * 15, seq=1, timestamp=1,
                              ssrc=1, extension=None)


# --- Transport и Digest -----------------------------------------------------

def test_parse_transport_tcp_interleaved():
    tr = rr.parse_transport("RTP/AVP/TCP;unicast;interleaved=2-3")
    assert tr.tcp and tr.interleaved == (2, 3)


def test_parse_transport_udp_needs_client_port():
    tr = rr.parse_transport("RTP/AVP;unicast;client_port=5000-5001")
    assert not tr.tcp and tr.client_ports == (5000, 5001)
    with pytest.raises(ValueError):
        rr.parse_transport("RTP/AVP;unicast")


def test_digest_response_matches_rfc2069():
    expected = hashlib.md5(":".join([
        hashlib.md5(b"onvif:realm:secret").hexdigest(),
        "nonce",
        hashlib.md5(b"DESCRIBE:rtsp://h/cam1").hexdigest(),
    ]).encode()).hexdigest()
    assert rr.digest_response("onvif", "secret", "realm", "nonce",
                              "DESCRIBE", "rtsp://h/cam1") == expected


def test_parse_digest_header_takes_quoted_and_bare():
    parsed = rr.parse_digest_header(
        'Digest username="onvif", nonce="abc", nc=00000001, response="deadbeef"')
    assert parsed["username"] == "onvif"
    assert parsed["nc"] == "00000001"
    assert parsed["response"] == "deadbeef"


# --- ffmpeg: только remux ---------------------------------------------------

def test_piece_args_never_transcode():
    """§24 «Явно вне рамок: перекодирование архива (только remux)».

    Тест сторожит именно это: `-c:v copy` в аргументах и отсутствие любого
    кодировщика. Перекодирование на воспроизведении съело бы те ядра,
    которые §16 отводит аналитике, и незаметно — VMS показывал бы картинку.
    """
    piece = rr.PlayPiece(path="/media/segments/cam1_1.mp4", start_offset=3.0,
                         duration=10.0, started_at=datetime(2026, 8, 19, 4, 10))
    args = rr.ffmpeg_piece_args(piece, 5000, payload_type=96, rate=1.0)
    assert "copy" in args
    joined = " ".join(args)
    for encoder in ("libx264", "libx265", "-crf", "-b:v"):
        assert encoder not in joined
    # -ss стоит ДО -i: вход по ключевому кадру, не позже запрошенного.
    assert args.index("-ss") < args.index("-i")


def test_piece_args_rate_control_off_drops_readrate():
    """`Rate-Control: no` — отдача на скорости диска, а не реального
    времени. Оставшийся `-readrate` растянул бы выгрузку часа записи на
    час."""
    piece = rr.PlayPiece(path="/media/segments/cam1_1.mp4", start_offset=0.0,
                         duration=10.0, started_at=datetime(2026, 8, 19, 4, 10))
    assert "-readrate" not in rr.ffmpeg_piece_args(piece, 5000, payload_type=96,
                                                   rate=None)
    assert "-readrate" in rr.ffmpeg_piece_args(piece, 5000, payload_type=96,
                                               rate=2.0)


# --- SDP --------------------------------------------------------------------

FFMPEG_SDP = """v=0
o=- 0 0 IN IP4 127.0.0.1
s=No Name
c=IN IP4 127.0.0.1
t=0 0
a=tool:libavformat 60.16.100
m=video 5000 RTP/AVP 96
b=AS:200
a=rtpmap:96 H264/90000
a=fmtp:96 packetization-mode=1; sprop-parameter-sets=Z0LgHtoCgPRA,aM4wpIA=; profile-level-id=42E01E
"""


def test_parse_ffmpeg_sdp_keeps_decoder_parameters():
    desc = rr.parse_ffmpeg_sdp(FFMPEG_SDP)
    assert desc.codec == "H264" and desc.clock_rate == 90000
    assert desc.payload_type == 96
    # Без sprop-parameter-sets клиент не соберёт декодер.
    assert any("sprop-parameter-sets" in line for line in desc.sdp_media_lines)


def test_build_sdp_announces_available_range():
    """`a=range:` — то, по чему VMS рисует, за какие сутки запись есть,
    ещё до первого PLAY."""
    desc = rr.parse_ffmpeg_sdp(FFMPEG_SDP)
    sdp = rr.build_sdp(desc, session_name="cam1", available=rr.ClockRange(
        datetime(2026, 8, 19, 4, 0), datetime(2026, 8, 19, 5, 0)))
    assert "a=range:clock=20260819T040000Z-20260819T050000Z" in sdp
    assert "a=control:trackID=0" in sdp
    assert "a=rtpmap:96 H264/90000" in sdp


# ============================================================================
# Настоящее воспроизведение
# ============================================================================

RTSP_CT = "application/sdp"


class RawRtspClient:
    """Минимальный RTSP-клиент поверх сокета.

    Умеет ровно то, что делает VMS: Digest, DESCRIBE, SETUP по TCP,
    PLAY с `Range: clock=` и чтение интерливинговых пакетов. Ничего не
    исправляет за сервер — в этом и смысл.
    """

    def __init__(self, host: str, port: int, path: str,
                 user: str, password: str) -> None:
        self.base = f"rtsp://{host}:{port}/{path}"
        self.user, self.password = user, password
        self.sock = socket.create_connection((host, port), timeout=15)
        self.buf = b""
        self.cseq = 0
        self.nonce = ""
        self.realm = ""
        self.session = ""

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass

    def _auth(self, method: str, uri: str) -> str:
        resp = rr.digest_response(self.user, self.password, self.realm,
                                  self.nonce, method, uri)
        return (f'Digest username="{self.user}", realm="{self.realm}", '
                f'nonce="{self.nonce}", uri="{uri}", response="{resp}"')

    def request(self, method: str, headers: dict | None = None,
                uri: str | None = None, authenticate: bool = True,
                retry_auth: bool = True):
        """Один запрос. На 401 — повтор с полученным nonce, ровно как это
        делает любой RTSP-клиент: nonce выдаётся сервером, и первый запрос
        по определению уходит без него."""
        result = self._send(method, headers, uri, authenticate)
        if result[0] == 401 and authenticate and retry_auth and self.nonce:
            result = self._send(method, headers, uri, True)
        return result

    def _send(self, method: str, headers: dict | None,
              uri: str | None, authenticate: bool):
        uri = uri or self.base
        self.cseq += 1
        head = dict(headers or {})
        if authenticate and self.nonce:
            head["Authorization"] = self._auth(method, uri)
        if self.session:
            head.setdefault("Session", self.session)
        lines = [f"{method} {uri} RTSP/1.0", f"CSeq: {self.cseq}"]
        lines += [f"{k}: {v}" for k, v in head.items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return self._read_response()

    def _fill(self, need: int) -> None:
        while len(self.buf) < need:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("сервер закрыл соединение")
            self.buf += chunk

    def _read_response(self):
        # Интерливинговые пакеты, пришедшие между ответами, откладываются.
        while True:
            self._fill(1)
            if self.buf[:1] != b"$":
                break
            self._fill(4)
            length = struct.unpack("!H", self.buf[2:4])[0]
            self._fill(4 + length)
            self.buf = self.buf[4 + length:]
        while b"\r\n\r\n" not in self.buf:
            self._fill(len(self.buf) + 1)
        head, rest = self.buf.split(b"\r\n\r\n", 1)
        text = head.decode("utf-8", "replace")
        status = int(text.split()[1])
        headers = {}
        for line in text.split("\r\n")[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        length = int(headers.get("content-length", "0") or 0)
        self.buf = rest
        if length:
            self._fill(length)
            body = self.buf[:length].decode("utf-8", "replace")
            self.buf = self.buf[length:]
        else:
            body = ""
        if status == 401 and "www-authenticate" in headers:
            params = rr.parse_digest_header(headers["www-authenticate"])
            self.nonce = params.get("nonce", "")
            self.realm = params.get("realm", "")
        if "session" in headers:
            self.session = headers["session"].split(";")[0]
        return status, headers, body

    def authenticate(self) -> None:
        """Взять nonce у сервера.

        Не через OPTIONS: он отвечает 200 без авторизации (клиент вправе
        спросить список методов до предъявления учётки), и nonce с него не
        приходит. Берётся первым же закрытым запросом.
        """
        status, _h, _b = self.request("DESCRIBE", {"Accept": RTSP_CT},
                                      authenticate=False, retry_auth=False)
        assert status == 401, f"закрытая операция ответила {status} без учётки"
        assert self.nonce, "сервер не выдал nonce в WWW-Authenticate"

    def read_packet(self, timeout: float = 20.0) -> bytes:
        """Один интерливинговый RTP-пакет."""
        deadline = time.monotonic() + timeout
        self.sock.settimeout(timeout)
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("пакет не пришёл")
            self._fill(4)
            if self.buf[:1] != b"$":
                # Внеочередной ответ — снять его и продолжить.
                self._read_response()
                continue
            length = struct.unpack("!H", self.buf[2:4])[0]
            self._fill(4 + length)
            packet = self.buf[4:4 + length]
            self.buf = self.buf[4 + length:]
            return packet


def parse_extension(packet: bytes):
    """(ntp_datetime, flags) из RTP-пакета с расширением ONVIF."""
    assert packet[0] & 0x10, "в пакете нет расширения"
    csrc = packet[0] & 0x0F
    off = 12 + 4 * csrc
    profile, words = struct.unpack("!HH", packet[off:off + 4])
    assert profile == 0xABAC, f"чужой профиль расширения: {profile:#x}"
    assert words == 3
    ntp = struct.unpack("!Q", packet[off + 4:off + 12])[0]
    flags = packet[off + 12]
    seconds = (ntp >> 32) - rr.NTP_EPOCH_DELTA
    frac = (ntp & 0xFFFFFFFF) / (1 << 32)
    return datetime.utcfromtimestamp(seconds + frac), flags


@pytest.fixture()
def make_archive(pg_conn, make_camera):
    """Фабрика настоящего архива: сегменты MP4 на диске + строки в Postgres.

    Принимает план `[(смещение от base в секундах, длительность), ...]` —
    длительность сегментов важна не только для содержания. Тесты на утечку
    процессов различают «ffmpeg убит» и «ffmpeg сам доиграл» только по
    времени, и на шестисекундном сегменте не различают вовсе: он успевает
    закончиться внутри окна ожидания. Это и показала верификация откатом —
    с вырезанным `kill` тесты оставались зелёными.
    """
    if not HAVE_FFMPEG:
        pytest.skip("нужен ffmpeg/ffprobe")
    made: list[tuple[int, list[str]]] = []
    # Время в прошлом и «круглое» — чтобы расхождение читалось глазами.
    base = datetime(2026, 8, 19, 4, 10, 0)

    def _make(name: str, plan):
        cam = make_camera(name)
        seg_dir = os.path.join(settings.MEDIA_PATH, "segments")
        os.makedirs(seg_dir, exist_ok=True)
        paths = []
        cur = pg_conn.cursor()
        for index, (offset, seconds) in enumerate(plan):
            started = base + timedelta(seconds=offset)
            path = os.path.join(seg_dir, f"cam{cam['id']}_replay_{index}.mp4")
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi",
                "-i", f"testsrc=size=320x240:rate=10:duration={seconds}",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                # Ключевой кадр раз в секунду: вход по `-ss` должен попадать
                # близко к запрошенной секунде, иначе тест мерил бы GOP.
                "-g", "10", "-pix_fmt", "yuv420p", path,
            ], check=True, capture_output=True)
            paths.append(path)
            cur.execute(
                "INSERT INTO video_segments (camera_id, started_at, ended_at,"
                " file_path, event_type, duration_sec, size_bytes)"
                " VALUES (%s, %s, %s, %s, 'continuous', %s, %s)",
                (cam["id"], started, started + timedelta(seconds=seconds), path,
                 seconds, os.path.getsize(path)))
        cur.close()
        made.append((cam["id"], paths))
        return {"camera_id": cam["id"], "base": base, "paths": paths}

    yield _make

    cur = pg_conn.cursor()
    for cam_id, paths in made:
        cur.execute("DELETE FROM video_segments WHERE camera_id = %s", (cam_id,))
        for path in paths:
            try:
                os.remove(path)
            except OSError:
                pass
    cur.close()


@pytest.fixture()
def archive(make_archive):
    """Два сегмента с дырой между ними.

    Дыра — то, из-за чего время в расширении нельзя считать сквозной
    шкалой (см. модуль `rtsp_replay`). Без неё тест не отличил бы
    правильную реализацию от той, что склеивает всё одним concat.
    """
    return make_archive("replay-e2e", [(0, 6), (20, 6)])


# Длительность сегмента для тестов на утечку процессов. Убитый ffmpeg
# исчезает за секунды, доигравший сам — не раньше этого срока, и разрыв
# между ними должен быть кратным, а не на грани: иначе тест зелен и там,
# где процесс никто не убивал (проверено откатом).
LEAK_SEGMENT_SEC = 60
LEAK_WAIT_SEC = 10


@pytest.fixture()
def long_archive(make_archive):
    return make_archive("replay-leak", [(0, LEAK_SEGMENT_SEC)])


class ServerHarness:
    """Сервер в своём потоке и своём event loop.

    Своя фабрика сессий — потому что пул asyncpg привязан к циклу, который
    его создал, а цикл приложения живёт в TestClient. В эксплуатации
    сервер поднимается прямо в цикле приложения (`main.lifespan`) и берёт
    общий пул; здесь подменяется только это.
    """

    def __init__(self, password: str = "replay-secret") -> None:
        self.password = password
        self.port = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server: rr.RtspReplayServer | None = None
        self._ready = threading.Event()

    def __enter__(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=30), "сервер не поднялся"
        return self

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)

        from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
        engine = create_async_engine(settings.DATABASE_URL, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        async def main():
            self._server = rr.RtspReplayServer(
                "127.0.0.1", 0, username=settings.ONVIF_G_USERNAME,
                password=self.password, session_factory=factory)
            await self._server.start()
            self.port = self._server.bound_port
            self._ready.set()
            while True:
                await asyncio.sleep(3600)

        try:
            loop.run_until_complete(main())
        except (asyncio.CancelledError, RuntimeError):
            pass
        finally:
            try:
                if self._server is not None:
                    loop.run_until_complete(self._server.stop())
                loop.run_until_complete(engine.dispose())
                # Транспорты подпроцессов закрываются в свой черёд; без
                # этого шага их `close()` попадает на уже закрытый цикл и
                # засоряет вывод прогона «Event loop is closed».
                loop.run_until_complete(asyncio.sleep(0.2))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    def __exit__(self, *exc):
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=15)


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_replay_streams_archive_from_requested_instant(archive):
    """Главная проверка §12: клиент просит секунду архива — и получает её.

    Смотрится не «пришли ли байты», а время в RTP-расширении: именно им
    VMS подписывает картинку, и поток с правильным видео и неправильным
    временем — это запись, приписанная не тому моменту.
    """
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port, f"cam{archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        try:
            cli.authenticate()
            status, headers, sdp = cli.request("DESCRIBE",
                                               {"Accept": RTSP_CT})
            assert status == 200, sdp
            assert "a=range:clock=" in sdp
            assert "m=video" in sdp

            status, headers, _ = cli.request(
                "SETUP", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
                uri=cli.base + "/trackID=0")
            assert status == 200
            assert "interleaved=0-1" in headers["transport"]
            assert cli.session

            # Просим со второй секунды первого сегмента.
            want = archive["base"] + timedelta(seconds=2)
            status, headers, _ = cli.request("PLAY", {
                "Range": f"clock={rr.format_clock_time(want)}-",
                "Require": "onvif-replay",
            })
            assert status == 200, headers
            assert headers["range"].startswith("clock=")

            when, flags = parse_extension(cli.read_packet())
            # Вход идёт по ключевому кадру НЕ ПОЗЖЕ запрошенного — как у
            # экспорта: потерять начало события хуже, чем отдать лишнюю
            # секунду до него. GOP здесь 1 с, отсюда и допуск.
            assert when <= want + timedelta(seconds=0.5), (when, want)
            assert when >= want - timedelta(seconds=1.5), (when, want)
            assert flags & rr.EXT_FLAG_C, "первый пакет не помечен ключевым"
        finally:
            cli.close()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_replay_time_survives_the_gap_between_segments(archive):
    """Дыра в записи не смещает время дальнейших кадров.

    Ровно то, ради чего на каждый сегмент поднимается свой ffmpeg. Если
    склеить сегменты одним `concat`, второй сегмент поедет на длину дыры
    (здесь — 14 секунд), и VMS покажет событие не в то время, оставаясь
    при этом полностью «рабочим» на вид.
    """
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port, f"cam{archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        try:
            cli.authenticate()
            cli.request("DESCRIBE", {"Accept": RTSP_CT})
            cli.request("SETUP", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
                        uri=cli.base + "/trackID=0")
            # Rate-Control: no — выгрузка на скорости диска, иначе тест
            # ждал бы 26 секунд реального времени.
            status, _h, _b = cli.request("PLAY", {
                "Range": f"clock={rr.format_clock_time(archive['base'])}-",
                "Rate-Control": "no",
                "Require": "onvif-replay",
            })
            assert status == 200

            second_segment_start = archive["base"] + timedelta(seconds=20)
            ssrcs, times, saw_discontinuity = set(), [], False
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                try:
                    packet = cli.read_packet(timeout=10)
                except (TimeoutError, ConnectionError):
                    break
                ssrcs.add(struct.unpack("!I", packet[8:12])[0])
                when, flags = parse_extension(packet)
                times.append(when)
                if flags & rr.EXT_FLAG_D and when >= second_segment_start - timedelta(seconds=1):
                    saw_discontinuity = True
                if when >= second_segment_start + timedelta(seconds=1):
                    break

            assert times, "не пришло ни одного пакета"
            assert len(ssrcs) == 1, (
                f"куски пришли разными потоками (SSRC {ssrcs}) — клиент "
                "перезапустил бы декодер на каждом сегменте")
            # Время второго сегмента — настенное, а не «первый + его длина».
            reached = max(times)
            assert reached >= second_segment_start, (
                f"до второго сегмента не дошли: {reached}")
            assert reached <= second_segment_start + timedelta(seconds=8), (
                f"время уехало на дыру: {reached} вместо ~{second_segment_start}")
            assert saw_discontinuity, "разрыв записи не помечен флагом D"
        finally:
            cli.close()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_replay_stream_actually_decodes(archive):
    """То, чего сырой клиент не проверяет: отданное — настоящее видео.

    Клиентом здесь работает ffprobe, то есть проверяется и совместимость
    SDP с чужим стеком, а не только наши байты.
    """
    with ServerHarness() as srv:
        url = (f"rtsp://{settings.ONVIF_G_USERNAME}:{srv.password}"
               f"@127.0.0.1:{srv.port}/cam{archive['camera_id']}")
        proc = subprocess.run([
            "ffprobe", "-hide_banner", "-loglevel", "error",
            "-rtsp_transport", "tcp",
            "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,width,height",
            "-of", "default=noprint_wrappers=1",
            "-timeout", "15000000", url,
        ], capture_output=True, text=True, timeout=90)
        assert proc.returncode == 0, proc.stderr
        assert "codec_name=h264" in proc.stdout, proc.stdout
        assert "width=320" in proc.stdout, proc.stdout


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_replay_rejects_unknown_require(archive):
    """Клиент, попросивший неизвестную возможность, обязан получить 551
    со списком непонятого — иначе он решит, что сервер её умеет."""
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port, f"cam{archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        try:
            cli.authenticate()
            status, headers, _ = cli.request(
                "DESCRIBE", {"Require": "onvif-replay, funky-feature"})
            assert status == 551, status
            assert "funky-feature" in headers.get("unsupported", "")
            assert "onvif-replay" not in headers.get("unsupported", "")
        finally:
            cli.close()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_replay_requires_credentials(archive):
    """Архив наружу без пароля не отдаётся ни при каком запросе."""
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port, f"cam{archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, "wrong-password")
        try:
            status, _h, _b = cli.request("DESCRIBE", authenticate=False)
            assert status == 401
            # Со сгенерированным (неверным) ответом — снова 401, а не поток.
            status, _h, _b = cli.request("DESCRIBE")
            assert status == 401
        finally:
            cli.close()


def _replay_ffmpeg_pids() -> set[int]:
    """PID'ы ffmpeg, отдающих RTP этому серверу."""
    out = subprocess.run(["pgrep", "-f", "rtp://127.0.0.1"],
                         capture_output=True, text=True)
    return {int(line) for line in out.stdout.split() if line.strip().isdigit()}


def _play_long(cli, base) -> set[int]:
    """Довести сессию до идущего потока и вернуть PID'ы её ffmpeg."""
    cli.authenticate()
    cli.request("DESCRIBE", {"Accept": RTSP_CT})
    cli.request("SETUP", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
                uri=cli.base + "/trackID=0")
    status, _h, _b = cli.request("PLAY", {
        "Range": f"clock={rr.format_clock_time(base)}-"})
    assert status == 200
    cli.read_packet()
    pids = _replay_ffmpeg_pids()
    assert pids, "поток идёт, а ffmpeg не запущен"
    return pids


def _await_gone(pids: set[int]) -> set[int]:
    deadline = time.monotonic() + LEAK_WAIT_SEC
    while time.monotonic() < deadline:
        alive = pids & _replay_ffmpeg_pids()
        if not alive:
            return set()
        time.sleep(0.3)
    return pids & _replay_ffmpeg_pids()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_teardown_leaves_no_ffmpeg_behind(long_archive):
    """§13 «graceful shutdown» и утечка процессов.

    Брошенная сессия — это не строка в словаре, а живой ffmpeg с открытым
    файлом сегмента. Сегмент здесь длиной в минуту, а ждём десять секунд:
    доиграть сам процесс за это время не может, поэтому исчезновение
    означает именно то, что проверяется.
    """
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port,
                            f"cam{long_archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        try:
            pids = _play_long(cli, long_archive["base"])
            status, _h, _b = cli.request("TEARDOWN")
            assert status == 200
        finally:
            cli.close()
        assert not _await_gone(pids), "ffmpeg пережил TEARDOWN"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_dropped_connection_stops_playback(long_archive):
    """Оборванный VMS не должен оставлять играющий ffmpeg до таймаута."""
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port,
                            f"cam{long_archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        pids = _play_long(cli, long_archive["base"])
        cli.close()  # обрыв без TEARDOWN
        assert not _await_gone(pids), "ffmpeg пережил обрыв соединения"


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_server_stop_kills_running_sessions(long_archive):
    """Остановка приложения не оставляет процессов-сирот (§13).

    Это тот же класс утечки, но по другому поводу: сессия жива и здорова,
    а гасится сервер целиком — как при рестарте бэкенда.
    """
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port,
                            f"cam{long_archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        pids = _play_long(cli, long_archive["base"])
    # Выход из `with` — остановка сервера со всеми сессиями.
    try:
        assert not _await_gone(pids), "ffmpeg пережил остановку сервера"
    finally:
        cli.close()


@pytest.mark.skipif(not HAVE_FFMPEG, reason="нужен ffmpeg")
def test_play_outside_recording_is_not_found(archive):
    """Запрос времени, за которое записи нет, — 404, а не пустой поток.

    Пустой «успешный» поток VMS показывает как чёрный экран, и оператор
    читает это как «камера сломалась», а не «в этот час не писалось».
    """
    with ServerHarness() as srv:
        cli = RawRtspClient("127.0.0.1", srv.port, f"cam{archive['camera_id']}",
                            settings.ONVIF_G_USERNAME, srv.password)
        try:
            cli.authenticate()
            cli.request("DESCRIBE", {"Accept": RTSP_CT})
            cli.request("SETUP", {"Transport": "RTP/AVP/TCP;unicast;interleaved=0-1"},
                        uri=cli.base + "/trackID=0")
            far_future = archive["base"] + timedelta(days=3)
            status, _h, _b = cli.request("PLAY", {
                "Range": f"clock={rr.format_clock_time(far_future)}-"})
            assert status == 404, status
        finally:
            cli.close()
