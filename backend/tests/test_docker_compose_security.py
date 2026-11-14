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


# Хост-порт настраивается через .env (`${VAR:-default}`), и `${...}` сам
# содержит двоеточие — поэтому наивный split(":") больше не годится: он
# рвёт биндинг не по границе IP/хост-порт/порт-контейнера, а по первому
# попавшемуся ":" внутри шаблона, из-за чего host_port переставал совпадать
# с UNAUTHENTICATED_INTERNAL_PORTS и проверка молча переставала что-либо
# проверять для этих строк (тест зелёный, но без реальной проверки).
# Порт контейнера (target) всегда остаётся простым числом в конце строки —
# на него и опираемся, а не на хост-порт, который теперь может быть шаблоном.
_BINDING_RE = re.compile(
    r"^(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}|localhost):)?"
    r"(?P<host_port>.+):(?P<target_port>\d+)$"
)


def test_compose_file_exists():
    assert COMPOSE_PATH.is_file()


def test_unauthenticated_services_not_published_on_all_interfaces():
    checked_ports = set()
    for binding in _host_port_bindings():
        match = _BINDING_RE.match(binding)
        if not match:
            continue  # не порт-биндинг (например, "pgdata:/var/lib/...")
        target_port = match.group("target_port")
        if target_port not in UNAUTHENTICATED_INTERNAL_PORTS:
            continue
        checked_ports.add(target_port)
        assert match.group("ip") in ("127.0.0.1", "localhost"), (
            f"Порт {target_port} (postgres/redis/mediamtx, без встроенной "
            f"аутентификации) не должен публиковаться на всех интерфейсах: {binding!r}"
        )
    # Страховка от того, что сам парсер сломается и молча ничего не найдёт
    # (как уже случилось однажды при переходе на настраиваемые хост-порты).
    assert checked_ports == UNAUTHENTICATED_INTERNAL_PORTS, (
        "парсер не нашёл все ожидаемые порты без аутентификации в "
        f"docker-compose.yml: найдено {checked_ports}, ожидалось "
        f"{UNAUTHENTICATED_INTERNAL_PORTS}"
    )
