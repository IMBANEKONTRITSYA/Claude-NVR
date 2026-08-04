"""ТЗ 13: "полное шифрование данных на уровне приложения при передаче по
сети (TLS/HTTPS)... даже внутри локальной сети рекомендуется". Проверяет,
что HTTPS настроен по умолчанию (самоподписанный сертификат генерируется
docker-entrypoint.sh при первом запуске), а не требует ручной настройки
администратором. Статические проверки текста конфигов — без реального
поднятия nginx (слишком тяжело для юнит-тестов backend)."""
from pathlib import Path

FRONTEND = Path(__file__).resolve().parents[2] / "frontend"


def test_entrypoint_generates_self_signed_cert_if_missing():
    entrypoint = (FRONTEND / "docker-entrypoint.sh").read_text(encoding="utf-8")
    assert "openssl req" in entrypoint
    assert "fullchain.pem" in entrypoint and "privkey.pem" in entrypoint
    # Не должен перегенерировать (и заново пугать браузер) при каждом старте
    assert "if [ ! -f" in entrypoint


def test_dockerfile_wires_entrypoint_and_installs_openssl():
    dockerfile = (FRONTEND / "Dockerfile").read_text(encoding="utf-8")
    assert "openssl" in dockerfile
    assert "docker-entrypoint.sh" in dockerfile
    assert 'ENTRYPOINT ["/docker-entrypoint.sh"]' in dockerfile
    assert "443" in dockerfile


def test_nginx_serves_https_with_certs():
    conf = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")
    assert "listen 443 ssl" in conf
    assert "ssl_certificate " in conf and "ssl_certificate_key " in conf
    assert "ssl_protocols TLSv1.2 TLSv1.3" in conf
    # HTTP остаётся доступен (без принудительного редиректа) — см. комментарий
    # в nginx.conf: однокомпьютерная LAN-система, редирект на самоподписанный
    # сертификат до явного согласия администратора добавил бы трение первому
    # запуску без реального выигрыша в безопасности.
    assert "listen 80" in conf


def test_compose_publishes_https_port_and_persists_cert():
    compose = (FRONTEND.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert '"8443:443"' in compose
    assert "frontend_certs:/etc/nginx/certs" in compose
