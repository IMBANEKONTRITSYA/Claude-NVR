"""Структурированное JSON-логирование (ТЗ 12: "структурированные логи (JSON)
с ротацией, уровни DEBUG/INFO/WARN/ERROR"). Ротация размера уже закрыта на
уровне Docker (x-logging в docker-compose.yml) — этот модуль закрывает
формат тела сообщения. Идентичная копия существует в worker/logging_utils.py
(backend и worker — отдельные Docker-образы без общего пакета)."""
import json
import logging
import os
import re
import sys
from copy import copy
from datetime import datetime, timezone

from uvicorn.logging import AccessFormatter

# archive/reports/snapshot/prometheus/ws-эндпоинты принимают access-токен как
# ?token=... (нужно там, куда нельзя послать заголовок Authorization —
# прямые ссылки, WS handshake, сторонний monitoring). Дефолтный access-лог
# uvicorn пишет query string как есть, то есть токен уходит в открытом виде
# в docker-логи (docs/reviews/REVIEW-2026-08-04T103308Z.md, рекомендация №1
# цикла 7). Тот же класс редактирования, что и в frontend/nginx.conf — там
# редактируется на уровне reverse proxy, здесь на уровне самого backend,
# чтобы токен не осел в логах ни на одном хопе.
_TOKEN_QS_RE = re.compile(r"([?&]token=)[^&\s]*")


class RedactedAccessFormatter(AccessFormatter):
    """uvicorn.logging.AccessFormatter, вырезающий значение ?token=... из
    request line перед форматированием — used via --log-config (см.
    logging_config.json и Dockerfile)."""

    def formatMessage(self, record: logging.LogRecord) -> str:
        recordcopy = copy(record)
        client_addr, method, full_path, http_version, status_code = recordcopy.args
        recordcopy.args = (
            client_addr,
            method,
            _TOKEN_QS_RE.sub(r"\1REDACTED", full_path),
            http_version,
            status_code,
        )
        return super().formatMessage(recordcopy)

# Стандартные атрибуты LogRecord — всё, что сверх них в record.__dict__,
# считается пользовательским полем (передано через logging.info(..., extra={...})).
_STANDARD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    """Одна JSON-строка на запись лога."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(name: str) -> logging.Logger:
    """Настраивает logger на вывод JSON-строк в stdout (подхватывается
    Docker json-file драйвером, ротация которого настроена в docker-compose.yml)."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False
    return logger
