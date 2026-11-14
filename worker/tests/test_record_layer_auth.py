"""Аутентификация в Control API MediaMTX (§2, §4, §5) — цикл 34.

**Что сломалось на боевом развёртывании.** Воркер получал
`HTTP 401 authentication error` на каждый вызов Control API, и вместе с
ним отваливались §4 и §5 целиком: пути камер не заводились, MediaMTX
ничего не тянул, live показывал чёрный экран на всех камерах, архив был
пуст, а статусы потоков не читались — все камеры навсегда оставались
`offline`.

**Почему.** MediaMTX по умолчанию (проверено на настоящих бинарниках
v1.9.3 и v1.16.0) выдаёт право `api` только записи `authInternalUsers` с
`ips: ['127.0.0.1', '::1']`. Воркер — отдельный контейнер и приходит с
адреса docker-сети. Healthcheck контейнера при этом ходит с loopback и
остаётся зелёным, поэтому отказ ничем не выдавал себя, кроме сообщения на
странице мониторинга.

Тесты — против настоящего HTTP-сервера, как и весь `test_record_layer.py`:
проверяется, что клиент шлёт именно `Authorization: Basic`. Query-параметры
`?user=&pass=`, которые MediaMTX принимает для HLS и WebRTC, для Control
API **не работают** — тоже проверено на v1.16.0, и поэтому здесь их нет.
"""
import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from record_layer import MediaMTXClient, MediaMTXError, redact_url, split_credentials

USER, PASSWORD = "facewatch", "s3cret:pass@word"


class _AuthenticatingMediaMTX(BaseHTTPRequestHandler):
    """Двойник, повторяющий поведение MediaMTX: без верных учётных данных —
    401 с тем же телом, что отдаёт настоящий сервер."""

    seen_authorization: list = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).seen_authorization.append(self.headers.get("Authorization"))
        expected = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        if self.headers.get("Authorization") != f"Basic {expected}":
            body = json.dumps({"status": "error", "error": "authentication error"}).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = json.dumps({"itemCount": 1, "pageCount": 1,
                           "items": [{"name": "cam1", "online": True}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def server():
    _AuthenticatingMediaMTX.seen_authorization = []
    srv = HTTPServer(("127.0.0.1", 0), _AuthenticatingMediaMTX)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield f"127.0.0.1:{srv.server_port}"
    finally:
        srv.shutdown()
        srv.server_close()


def test_credentials_from_url_open_the_api(server):
    """Учётка едет внутри `MEDIAMTX_API_URL` — так же, как она задана в
    docker-compose.yml."""
    from urllib.parse import quote

    url = f"http://{USER}:{quote(PASSWORD, safe='')}@{server}"
    assert MediaMTXClient(url).runtime_paths() == {"cam1": {"name": "cam1", "online": True}}


def test_without_credentials_the_api_answers_401(server):
    """Проверка откатом: тот же клиент без учётки получает ровно то, что
    видел пользователь на боевом сервере."""
    with pytest.raises(MediaMTXError) as e:
        MediaMTXClient(f"http://{server}").runtime_paths()
    assert "401" in str(e.value)
    assert _AuthenticatingMediaMTX.seen_authorization == [None]


def test_401_message_names_the_place_to_fix(server):
    """Голое «authentication error» видит оператор на странице мониторинга.
    Сообщение обязано называть и файл, и переменную."""
    with pytest.raises(MediaMTXError) as e:
        MediaMTXClient(f"http://wrong:wrong@{server}").runtime_paths()
    text = str(e.value)
    assert "authInternalUsers" in text and "MEDIAMTX_API_URL" in text
    assert "action: api" in text


def test_authorization_header_goes_with_the_first_request(server):
    """Не после 401, как это делает HTTPBasicAuthHandler: синхронизация
    путей идёт каждые ~10 с и удваивать её запросы нельзя."""
    from urllib.parse import quote

    MediaMTXClient(f"http://{USER}:{quote(PASSWORD, safe='')}@{server}").runtime_paths()
    assert len(_AuthenticatingMediaMTX.seen_authorization) == 1
    assert _AuthenticatingMediaMTX.seen_authorization[0].startswith("Basic ")


@pytest.mark.parametrize("url, expected_host, expected_creds", [
    ("http://mediamtx:9997", "http://mediamtx:9997", None),
    ("http://mediamtx:9997/", "http://mediamtx:9997", None),
    ("http://u:p@mediamtx:9997", "http://mediamtx:9997", ("u", "p")),
    # Пароль со спецсимволами приезжает из .env percent-encoded.
    ("http://u:p%40ss%3Aword@mediamtx:9997", "http://mediamtx:9997", ("u", "p@ss:word")),
    ("http://u@mediamtx:9997", "http://mediamtx:9997", ("u", "")),
])
def test_split_credentials(url, expected_host, expected_creds):
    assert split_credentials(url) == (expected_host, expected_creds)


def test_redact_url_hides_the_password():
    """Адрес попадает в лог воркера и оттуда — в сообщение об ошибке на
    странице мониторинга. Пароль слоя записи оператору там не место."""
    assert redact_url("http://facewatch:s3cret@mediamtx:9997") == "http://facewatch:***@mediamtx:9997"
    assert "s3cret" not in redact_url("http://facewatch:s3cret@mediamtx:9997")
    assert redact_url("http://mediamtx:9997") == "http://mediamtx:9997"
