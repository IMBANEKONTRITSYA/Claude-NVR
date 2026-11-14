"""ТЗ 13: "Reverse proxy (Nginx) с настройками безопасности (заголовки,
rate limiting)" — было полностью не реализовано, обнаружено при написании
docs/API_DOCS.md. Статические проверки текста конфигов — без реального
поднятия nginx (слишком тяжело для юнит-тестов backend), см. также
test_tls_config.py для того же подхода.

Цикл 21: сами заголовки переехали из nginx-locations.conf в общий сниппет
nginx-security-headers.conf — иначе их нельзя включить ещё и внутрь
`location /hls/`, где наследование add_header не работает (полное
объяснение — test_nginx_security_headers.py). Проверки ниже читают тот
файл, где заголовки объявлены теперь; расстановка include по блокам —
предмет соседнего модуля, здесь проверяется содержание.
"""
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
HEADERS_SNIPPET = FRONTEND / "nginx-security-headers.conf"


def test_security_headers_present_on_both_listeners():
    locations = HEADERS_SNIPPET.read_text(encoding="utf-8")
    # Общий snippet подключается и в HTTP (80), и в HTTPS (443) server{} —
    # заголовки, применимые к обоим, живут здесь, а не дублируются в nginx.conf.
    for header in ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in locations
        # "always" — заголовок должен уйти и на error-ответах (4xx/5xx), не
        # только на успешных; без него add_header молчит на них.
        line = next(l for l in locations.splitlines() if f"add_header {header}" in l)
        assert "always" in line


def test_csp_header_present_and_permits_hls_worker():
    # ТЗ 13: OWASP Top 10 — защита от XSS. worker-src blob: — без него
    # hls.js (enableWorker по умолчанию) не может создать свой demuxer-
    # воркер через URL.createObjectURL(new Blob(...)), браузер блокирует
    # его с "Refused to create a worker from 'blob:...' because it violates
    # ... script-src" — подтверждено вручную headless Chromium: без
    # worker-src видео на LiveGrid не воспроизводится вообще (см. PR).
    locations = HEADERS_SNIPPET.read_text(encoding="utf-8")
    line = next(l for l in locations.splitlines() if "add_header Content-Security-Policy" in l)
    assert "always" in line
    assert "default-src 'self'" in line
    assert "worker-src 'self' blob:" in line
    # Никаких внешних хостов (CDN и т.п.) — фронтенд не использует их вовсе,
    # 'self' везде не должен получить исключений вида *.example.com.
    assert "object-src 'none'" in line
    assert "frame-ancestors 'none'" in line


def test_hsts_only_on_https_listener():
    """HSTS уходит только по HTTPS — теперь через значение из `map $https`.

    До цикла 21 это выражалось расположением: `add_header` стоял прямо в
    443-м server{}. Так его нельзя переиспользовать в общем сниппете,
    который надо включать и внутрь `location /hls/`, поэтому условие
    переехало из расположения в значение: на HTTP `$hsts_value` пуст, а
    add_header с пустым значением nginx не отправляет. Проверено на живом
    nginx 1.24 — по HTTP заголовка нет, по HTTPS есть.
    """
    snippet = HEADERS_SNIPPET.read_text(encoding="utf-8")
    line = next(l for l in snippet.splitlines()
                if "add_header Strict-Transport-Security" in l)
    assert "always" in line
    assert "$hsts_value" in line, "значение HSTS должно приходить из map $https"

    conf = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")
    # map объявлен в http{}-контексте, то есть до обоих server{}.
    assert 'map $https $hsts_value' in conf
    map_block = conf.split("map $https $hsts_value", 1)[1].split("}", 1)[0]
    assert 'default ""' in map_block, "на HTTP значение обязано быть пустым"
    assert "max-age=63072000" in map_block
    assert "includeSubDomains" in map_block
    # Прежней безусловной константы в server{} остаться не должно — иначе
    # заголовок ушёл бы дважды.
    assert 'add_header Strict-Transport-Security "' not in conf


def test_hls_location_requires_auth_request():
    # P0 (цикл 6): /hls/ проксировал в MediaMTX без какой-либо проверки —
    # любой с сетевым доступом к nginx мог смотреть любую камеру без логина.
    locations = (FRONTEND / "nginx-locations.conf").read_text(encoding="utf-8")
    assert "location = /internal/hls-auth" in locations
    # Комментарии в этом конфиге содержат `}` (например, "cam{id}") — split
    # по первой "}" отрезал бы блок раньше времени, поэтому режем по началу
    # следующего location-блока, а не по скобке.
    internal_start = locations.index("location = /internal/hls-auth")
    hls_start = locations.index("location /hls/", internal_start)
    internal_block = locations[internal_start:hls_start]
    assert "internal;" in internal_block

    next_location_start = locations.index("location / {", hls_start)
    hls_block = locations[hls_start:next_location_start]
    assert "auth_request /internal/hls-auth" in hls_block


def test_login_endpoint_is_rate_limited():
    conf = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")
    assert "limit_req_zone" in conf
    assert "zone=login" in conf
    locations = (FRONTEND / "nginx-locations.conf").read_text(encoding="utf-8")
    assert "location = /api/auth/login" in locations
    login_block = locations.split("location = /api/auth/login", 1)[1].split("}", 1)[0]
    assert "limit_req zone=login" in login_block
    # Общий /api/ location не должен унаследовать этот лимит — он для всех
    # остальных эндпоинтов, не только логина.
    general_api_block = locations.split("location /api/ {", 1)[1].split("}", 1)[0]
    assert "limit_req" not in general_api_block
