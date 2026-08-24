"""Пул постоянных соединений nginx → бэкенд (§19: запас по ресурсам).

Зачем набор. Без пула `proxy_pass http://host:port` ходит к апстриму по
HTTP/1.0 с `Connection: close` — каждый запрос к /api/ открывает новое
TCP-соединение и закрывает его; закрывает активно nginx, поэтому порт
уходит в TIME_WAIT на 60 секунд (2×MSL, в Linux не настраивается).

Замерено на настоящем nginx 1.24 перед настоящим бэкендом (цикл 62,
1000 запросов, 8 конкурентных клиентов, `/api/health`):

    HTTP/1.0 без пула      : 318.6 rps, TIME_WAIT +1037 на 1000 запросов
    HTTP/1.1 + keepalive 32: 350.7 rps, TIME_WAIT       0, пул из 16

Дело не столько в задержке (TCP-connect по петле дёшев; на объекте
разница будет больше), сколько в исчерпании ресурса: диапазон эфемерных
портов по умолчанию 28232 (32768-60999) при TIME_WAIT 60 с даёт потолок
устойчивого темпа ~470 запросов/с — дальше nginx не может подключиться
(EADDRNOTAVAIL) и отвечает 502, причём под нагрузкой.

**Главное, что здесь сторожится, — что директив ровно три и они вместе.**
Объявить `keepalive` в `upstream` и забыть `proxy_http_version 1.1` +
`proxy_set_header Connection ""` — классическая ошибка: конфиг проходит
`nginx -t`, выглядит правильным, и не даёт ничего. Проверено откатом на
живом nginx: с одним лишь `keepalive 32` в upstream — TIME_WAIT +1014 на
1000 запросов и **ноль** соединений в пуле, ровно как без правки.
Ошибка молчаливая, поэтому нужен тест, а не комментарий.

Проверки статические (текст конфигов), как в test_nginx_hardening.py и
test_tls_config.py: поднимать nginx в юнит-тестах бэкенда слишком тяжело.
"""
import re
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
NGINX_CONF = FRONTEND / "nginx.conf"
LOCATIONS = FRONTEND / "nginx-locations.conf"

# location'ы, которые проксируют в бэкенд обычным HTTP и обязаны
# переиспользовать соединения из пула.
POOLED_LOCATIONS = (
    "location = /api/auth/login",
    "location /api/",
    "location = /internal/hls-auth",
)


def _block(text: str, header: str) -> str:
    """Тело location-блока по его заголовку (до закрывающей скобки)."""
    start = text.index(header)
    depth, i = 0, start
    while True:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
        i += 1


def test_upstream_block_declares_keepalive_pool():
    conf = NGINX_CONF.read_text(encoding="utf-8")
    assert "upstream facewatch_backend" in conf, (
        "апстрим бэкенда должен быть объявлен блоком upstream — без него "
        "директиву keepalive поставить некуда"
    )
    block = _block(conf, "upstream facewatch_backend")
    assert "server facewatch-backend:8000;" in block
    m = re.search(r"^\s*keepalive\s+(\d+);", block, re.M)
    assert m, "в upstream нет директивы keepalive — пул не создаётся"
    assert int(m.group(1)) >= 16, (
        "потолок простаивающих соединений в пуле меньше разумной "
        "конкурентности одного воркера"
    )


def test_pooled_locations_pass_to_upstream_not_to_hostport():
    """Прямой `proxy_pass http://host:port` идёт мимо пула.

    Пул привязан к имени upstream'а; обращение по host:port создаёт
    безымянный апстрим без keepalive, даже если рядом объявлен нужный.
    """
    locations = LOCATIONS.read_text(encoding="utf-8")
    for header in POOLED_LOCATIONS:
        block = _block(locations, header)
        assert "facewatch_backend" in block, f"{header}: proxy_pass мимо пула"
        assert "facewatch-backend:8000" not in block, (
            f"{header}: proxy_pass по host:port — соединения не попадут в пул"
        )


def test_pooled_locations_set_http_11_and_clear_connection():
    """Обе директивы обязательны — по отдельности пул не работает.

    Это и есть то, что провалилось при проверке откатом: `keepalive` в
    upstream без этих двух строк даёт ноль переиспользованных соединений.
    """
    locations = LOCATIONS.read_text(encoding="utf-8")
    for header in POOLED_LOCATIONS:
        block = _block(locations, header)
        assert "proxy_http_version 1.1;" in block, (
            f"{header}: без HTTP/1.1 nginx закрывает соединение к апстриму "
            f"на каждом запросе, keepalive не действует"
        )
        assert re.search(r'proxy_set_header\s+Connection\s+"";', block), (
            f'{header}: без `proxy_set_header Connection ""` клиентский '
            f"заголовок Connection доезжает до апстрима и рвёт пул"
        )


def test_websocket_location_keeps_upgrade_semantics():
    """У /ws/ Connection несёт "upgrade" — обнулять его там нельзя.

    Соединение после апгрейда перестаёт быть HTTP и в пул вернуться не
    может; правка пула не должна была его задеть. Проверено вживую:
    апгрейд на /ws/faces и /ws/cameras проходит, приходит ping.
    """
    locations = LOCATIONS.read_text(encoding="utf-8")
    block = _block(locations, "location /ws/")
    assert "proxy_http_version 1.1;" in block
    assert 'proxy_set_header Connection "upgrade";' in block
    assert 'proxy_set_header Connection "";' not in block, (
        "обнуление Connection в /ws/ сломало бы апгрейд протокола"
    )
    assert "proxy_set_header Upgrade $http_upgrade;" in block


def test_hls_location_still_goes_to_mediamtx():
    """Пул бэкенда не должен был перехватить видеопоток.

    /hls/ проксируется в MediaMTX, а не в бэкенд, — если бы правка
    заменила и его апстрим, live-просмотр §4 отдавал бы 404 от FastAPI.
    """
    locations = LOCATIONS.read_text(encoding="utf-8")
    block = _block(locations, "location /hls/")
    assert "facewatch-mediamtx:8888" in block
    assert "facewatch_backend" not in block
