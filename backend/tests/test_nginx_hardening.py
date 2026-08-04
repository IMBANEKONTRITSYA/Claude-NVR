"""ТЗ 13: "Reverse proxy (Nginx) с настройками безопасности (заголовки,
rate limiting)" — было полностью не реализовано, обнаружено при написании
docs/API_DOCS.md. Статические проверки текста конфигов — без реального
поднятия nginx (слишком тяжело для юнит-тестов backend), см. также
test_tls_config.py для того же подхода."""
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_security_headers_present_on_both_listeners():
    locations = (FRONTEND / "nginx-locations.conf").read_text(encoding="utf-8")
    # Общий snippet подключается и в HTTP (80), и в HTTPS (443) server{} —
    # заголовки, применимые к обоим, живут здесь, а не дублируются в nginx.conf.
    for header in ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy"):
        assert header in locations
        # "always" — заголовок должен уйти и на error-ответах (4xx/5xx), не
        # только на успешных; без него add_header молчит на них.
        line = next(l for l in locations.splitlines() if f"add_header {header}" in l)
        assert "always" in line


def test_hsts_only_on_https_listener():
    conf = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")
    assert "Strict-Transport-Security" in conf
    https_block = conf.split("listen 443 ssl", 1)[1]
    assert "Strict-Transport-Security" in https_block
    http_block = conf.split("listen 443 ssl", 1)[0]
    # HSTS не должен попасть в общий snippet и не должен стоять в блоке 80 —
    # браузер игнорирует его по HTTP, но явное отсутствие проверяем и тут.
    assert "Strict-Transport-Security" not in http_block


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
