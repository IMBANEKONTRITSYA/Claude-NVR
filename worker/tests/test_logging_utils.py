import io
import json
import logging

from logging_utils import JsonFormatter, configure_logging


def _capture(logger, level, msg, **kwargs):
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    getattr(logger, level)(msg, **kwargs)
    return json.loads(stream.getvalue().strip())


def test_json_formatter_basic_fields():
    logger = logging.getLogger("test.basic")
    logger.setLevel(logging.INFO)
    record = _capture(logger, "info", "камера подключена", extra={"camera_id": 3})
    assert record["level"] == "INFO"
    assert record["logger"] == "test.basic"
    assert record["message"] == "камера подключена"
    assert record["camera_id"] == 3
    assert "timestamp" in record


def test_json_formatter_exception_field():
    logger = logging.getLogger("test.exc")
    logger.setLevel(logging.ERROR)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    try:
        raise ValueError("boom")
    except ValueError:
        logger.error("сбой", exc_info=True)
    record = json.loads(stream.getvalue().strip())
    assert "ValueError: boom" in record["exception"]


def test_json_formatter_no_extra_leaks_internal_attrs():
    logger = logging.getLogger("test.clean")
    logger.setLevel(logging.INFO)
    record = _capture(logger, "info", "просто сообщение")
    # Не должно быть служебных полей LogRecord вроде pathname/lineno/msg
    assert set(record.keys()) == {"timestamp", "level", "logger", "message"}


def test_configure_logging_sets_level_from_env(monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    logger = configure_logging("facewatch.worker.test")
    assert logger.level == logging.DEBUG
    assert len(logger.handlers) == 1
    assert isinstance(logger.handlers[0].formatter, JsonFormatter)


def test_configure_logging_is_idempotent():
    configure_logging("facewatch.worker.test2")
    logger = configure_logging("facewatch.worker.test2")
    assert len(logger.handlers) == 1
