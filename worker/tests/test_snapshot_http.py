"""Снимок камеры скачивается с учётными данными, которые лежат внутри URI.

Находка цикла 19. GetSnapshotUri отдаёт адрес без учётных данных, а
inject_credentials() подставляет их прямо в URL — так надо ffmpeg и
OpenCV, которые иначе логин с паролем не увидят. Прежняя реализация
скармливала этот же URL в urllib.request.urlopen(), а тот userinfo не
разбирает: строка "user:pass@192.168.1.10" уходила в резолвер целиком как
имя хоста и падала с `URLError: Name or service not known`. Дальше
`except Exception: return None` гасил отказ, и снимок молча резался из
кадра аналитики 640×360 — ровно то, ради устранения чего в цикле 18
делались снимки в полном разрешении. Симптома не было никакого: фича
просто не работала ни на одной камере с паролем, то есть на всех реальных.

Тесты поднимают настоящий HTTP-сервер (http.server из stdlib) и проверяют
полный production path скачивания: реальный сокет, реальный ответ 401 с
WWW-Authenticate, реальный повторный запрос с заголовком Authorization —
не мок urlopen. Мокать здесь было бы бессмысленно: находка ровно в том,
как stdlib обходится с URL, и мок воспроизвёл бы предположение автора, а
не поведение urllib.

Модуль snapshot_http сознательно не тянет cv2/insightface, поэтому эти
тесты идут в CI-джобе воркера (см. .github/workflows/ci.yml), в отличие от
тестов самого worker.py.
"""
import base64
import hashlib
import http.server
import threading

import pytest

from snapshot_http import build_opener, fetch_snapshot_bytes, split_credentials

JPEG = b"\xff\xd8\xff\xe0stub-jpeg-body"
USER, PASSWORD = "admin", "s3cr3t"
REALM = "IP Camera"


class _Handler(http.server.BaseHTTPRequestHandler):
    """Камера, требующая аутентификации. Схема задаётся классом-наследником."""

    scheme = "Basic"
    seen_authorization: list[str] = []

    def do_GET(self):
        auth = self.headers.get("Authorization")
        if auth:
            type(self).seen_authorization.append(auth)
        if not self._authorized(auth):
            self.send_response(401)
            self.send_header("WWW-Authenticate", self._challenge())
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(JPEG)))
        self.end_headers()
        self.wfile.write(JPEG)

    def _challenge(self):
        return f'{self.scheme} realm="{REALM}"'

    def _authorized(self, auth):
        raise NotImplementedError

    def log_message(self, *args):
        pass


class _BasicHandler(_Handler):
    scheme = "Basic"
    seen_authorization: list[str] = []

    def _authorized(self, auth):
        if not auth or not auth.startswith("Basic "):
            return False
        decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
        return decoded == f"{USER}:{PASSWORD}"


class _DigestHandler(_Handler):
    """Минимальная проверка Digest (qop=auth, MD5) — ровно столько, чтобы
    убедиться, что клиент действительно посчитал ответ по паролю, а не
    прислал что-то произвольное."""

    scheme = "Digest"
    nonce = "abc123nonce"
    seen_authorization: list[str] = []

    def _challenge(self):
        return f'Digest realm="{REALM}", qop="auth", nonce="{self.nonce}", algorithm=MD5'

    def _authorized(self, auth):
        if not auth or not auth.startswith("Digest "):
            return False
        fields = {}
        for part in auth[len("Digest "):].split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            fields[k.strip()] = v.strip().strip('"')
        if fields.get("username") != USER:
            return False
        ha1 = hashlib.md5(f"{USER}:{REALM}:{PASSWORD}".encode()).hexdigest()
        ha2 = hashlib.md5(f"GET:{fields.get('uri', '')}".encode()).hexdigest()
        expected = hashlib.md5(
            f"{ha1}:{fields.get('nonce', '')}:{fields.get('nc', '')}:"
            f"{fields.get('cnonce', '')}:{fields.get('qop', '')}:{ha2}".encode()
        ).hexdigest()
        return fields.get("response") == expected


@pytest.fixture(params=[_BasicHandler, _DigestHandler], ids=["basic", "digest"])
def camera(request):
    """Настоящий HTTP-сервер, отвечающий как камера с аутентификацией."""
    handler = request.param
    handler.seen_authorization = []
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield srv.server_address[1], handler
    finally:
        srv.shutdown()
        srv.server_close()


def test_credentials_in_uri_are_used_for_http_auth(camera):
    """Ядро находки: адрес с учётными данными внутри должен приводить к
    успешному скачиванию, а не к отказу резолва имени."""
    port, handler = camera
    url = f"http://{USER}:{PASSWORD}@127.0.0.1:{port}/onvif/snapshot"

    assert fetch_snapshot_bytes(url, timeout=5) == JPEG
    assert handler.seen_authorization, "камера должна была получить заголовок Authorization"


def test_wrong_password_is_rejected_not_silently_accepted(camera):
    """Неверный пароль — отказ (None), а не случайно прошедший запрос."""
    port, _ = camera
    url = f"http://{USER}:wrong-password@127.0.0.1:{port}/onvif/snapshot"
    assert fetch_snapshot_bytes(url, timeout=5) is None


def test_url_without_credentials_still_works():
    """Камера без пароля (или адрес, куда учётные данные не подставлялись)
    должна скачиваться обычным GET без всякой аутентификации."""

    class _Open(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(JPEG)))
            self.end_headers()
            self.wfile.write(JPEG)

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), _Open)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/snap.jpg"
        assert fetch_snapshot_bytes(url, timeout=5) == JPEG
    finally:
        srv.shutdown()
        srv.server_close()


def test_unreachable_camera_returns_none_and_warns(caplog):
    """Недоступная камера не роняет нить, но и не молчит: до фикса отказ
    гасился `except Exception: return None` без единой строчки в логе, и
    сломанный путь снимков был неотличим от камеры без GetSnapshotUri."""
    # Порт 9 (discard) на localhost закрыт — соединение отвергается сразу.
    url = f"http://{USER}:{PASSWORD}@127.0.0.1:9/snapshot"
    with caplog.at_level("WARNING"):
        assert fetch_snapshot_bytes(url, timeout=2) is None
    assert any("снимок с камеры недоступен" in r.message for r in caplog.records)


def test_warning_never_contains_the_password(caplog):
    """В лог уходит адрес без учётных данных: журнал воркера не должен
    становиться местом утечки пароля от камеры (ТЗ 13)."""
    url = f"http://{USER}:{PASSWORD}@127.0.0.1:9/snapshot"
    with caplog.at_level("WARNING"):
        fetch_snapshot_bytes(url, timeout=2)
    logged = " ".join(r.message + str(getattr(r, "snapshot_url", "")) for r in caplog.records)
    assert PASSWORD not in logged
    assert USER not in logged


def test_split_credentials_decodes_percent_encoding():
    """inject_credentials() кодирует логин и пароль через quote(safe=""),
    чтобы пароль вида "p@ss:w/ord" не развалил разбор URL, — обратное
    преобразование должно вернуть ровно исходное значение."""
    url = "http://admin:p%40ss%3Aw%2Ford@192.168.1.10:8080/snap?x=1"
    clean, user, password = split_credentials(url)
    assert clean == "http://192.168.1.10:8080/snap?x=1"
    assert user == "admin"
    assert password == "p@ss:w/ord"


def test_build_opener_strips_credentials_from_request_url():
    """Адрес, который реально уходит в сокет, не должен содержать userinfo:
    именно он попадает в Host-заголовок и в логи прокси/камеры."""
    clean, _ = build_opener("http://admin:s3cr3t@192.168.1.10/onvif/snapshot")
    assert clean == "http://192.168.1.10/onvif/snapshot"
    assert "s3cr3t" not in clean
