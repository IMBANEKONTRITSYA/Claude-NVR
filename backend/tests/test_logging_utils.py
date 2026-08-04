import io
import json
import logging

from app.logging_utils import JsonFormatter, configure_logging


def test_json_formatter_basic_fields():
    logger = logging.getLogger("test.backend.basic")
    logger.setLevel(logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]

    logger.info("администратор вошёл", extra={"user_id": 1, "ip": "127.0.0.1"})

    record = json.loads(stream.getvalue().strip())
    assert record["level"] == "INFO"
    assert record["logger"] == "test.backend.basic"
    assert record["message"] == "администратор вошёл"
    assert record["user_id"] == 1
    assert record["ip"] == "127.0.0.1"
    assert "timestamp" in record


def test_json_formatter_exception_field():
    logger = logging.getLogger("test.backend.exc")
    logger.setLevel(logging.ERROR)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    try:
        raise RuntimeError("миграция не удалась")
    except RuntimeError:
        logger.error("не удалось создать HNSW-индекс", exc_info=True)
    record = json.loads(stream.getvalue().strip())
    assert "RuntimeError" in record["exception"]


def test_configure_logging_sets_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    logger = configure_logging("facewatch.backend.test")
    assert logger.level == logging.WARNING
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)
