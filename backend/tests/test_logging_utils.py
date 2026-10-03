import io
import json
import logging

from app.logging_utils import JsonFormatter, RedactedAccessFormatter, configure_logging


def _format_access_line(full_path: str) -> str:
    formatter = RedactedAccessFormatter(fmt='%(client_addr)s - "%(request_line)s" %(status_code)s')
    record = logging.LogRecord(
        name="uvicorn.access", level=logging.INFO, pathname="", lineno=0,
        msg="", args=None, exc_info=None,
    )
    record.args = ("127.0.0.1:12345", "GET", full_path, "1.1", 200)
    return formatter.formatMessage(record)


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


def test_redacted_access_formatter_masks_token_value():
    line = _format_access_line("/api/reports/persons_csv?days=7&token=eyJhbGciOiJIUzI1NiJ9.secret&x=1")
    assert "token=REDACTED" in line
    assert "secret" not in line
    assert "days=7" in line and "x=1" in line


def test_redacted_access_formatter_masks_token_as_only_param():
    line = _format_access_line("/api/cameras/1/snapshot?token=SUPERSECRETVALUE")
    assert "token=REDACTED" in line
    assert "SUPERSECRETVALUE" not in line


def test_redacted_access_formatter_leaves_other_params_untouched():
    line = _format_access_line("/api/foo?mytoken=shouldstay&notoken=alsostay")
    assert "mytoken=shouldstay" in line
    assert "notoken=alsostay" in line


def test_redacted_access_formatter_leaves_paths_without_token_untouched():
    line = _format_access_line("/api/persons?page=2")
    assert "page=2" in line
    assert "REDACTED" not in line
