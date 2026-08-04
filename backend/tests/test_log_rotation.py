"""ТЗ 12: "структурированные логи (JSON) с ротацией". Docker-compose's
default json-file log driver has no size cap — on a system meant to run
24/7 (SPEC.md: "стабильно работать 24/7 без деградации"), that's a slow but
certain disk-fill. Every service must cap its log size.

Text-based checks (no PyYAML import), matching the existing convention in
test_docker_compose_security.py — keeps this test independent of whatever
transitive dependency happens to pull PyYAML into the environment."""
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[2] / "docker-compose.yml"


def _compose_text() -> str:
    return COMPOSE_PATH.read_text(encoding="utf-8")


def test_shared_logging_anchor_bounds_size_and_file_count():
    text = _compose_text()
    assert "x-logging: &default-logging" in text
    assert 'max-size: "10m"' in text
    assert 'max-file: "10"' in text


def test_every_service_references_the_logging_anchor():
    text = _compose_text()
    service_count = text.count("container_name: facewatch-")
    logging_ref_count = text.count("logging: *default-logging")
    assert service_count > 0
    assert logging_ref_count == service_count, (
        f"{service_count} сервисов, но только {logging_ref_count} ссылаются на "
        "*default-logging — у кого-то логи растут без ограничения"
    )
