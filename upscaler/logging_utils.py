"""Структурированное JSON-логирование (ТЗ 12: "структурированные логи (JSON)
с ротацией, уровни DEBUG/INFO/WARN/ERROR"). Ротация размера уже закрыта на
уровне Docker (x-logging в docker-compose.yml) — этот модуль закрывает
формат тела сообщения. Идентичная копия существует в worker/logging_utils.py
и backend/app/logging_utils.py: backend, worker и upscaler собираются в три
отдельных Docker-образа со своим контекстом сборки, общего пакета между ними
нет. Без тяжёлых зависимостей — импортируется и там, где есть только
стандартная библиотека."""
import json
import logging
import os
import sys
from datetime import datetime, timezone

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
