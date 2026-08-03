"""docker-compose.yml не должен публиковать порты сервисов без встроенной
аутентификации (postgres, redis, mediamtx) на все интерфейсы (ТЗ 13:
«файрволл, ограничение доступа»). backend/frontend публикуют собственную
аутентификацию поверх HTTP и намеренно остаются доступны отовсюду."""
import re
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"

# host_port: True, если публикация порта на все интерфейсы допустима
# (сервис сам проверяет аутентификацию/авторизацию запросов).
UNAUTHENTICATED_INTERNAL_PORTS = {"5432", "6379", "8554", "8888", "8889"}


def _host_port_bindings():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    # Строки вида '- "5432:5432"' или '- "127.0.0.1:5432:5432"'
    return re.findall(r'^\s*-\s*"([^"]+)"\s*(?:#.*)?$', text, re.MULTILINE)


def test_compose_file_exists():
    assert COMPOSE_PATH.is_file()


def test_unauthenticated_services_not_published_on_all_interfaces():
    for binding in _host_port_bindings():
        parts = binding.split(":")
        if len(parts) < 2:
            continue  # не порт-биндинг (например, "pgdata:/var/lib/...")
        host_port = parts[-2]
        if host_port not in UNAUTHENTICATED_INTERNAL_PORTS:
            continue
        assert len(parts) == 3 and parts[0] in ("127.0.0.1", "localhost"), (
            f"Порт {host_port} (postgres/redis/mediamtx, без встроенной "
            f"аутентификации) не должен публиковаться на всех интерфейсах: {binding!r}"
        )
