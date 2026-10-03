"""Координация graceful shutdown воркера (ТЗ 12). Отдельный модуль без
тяжёлых зависимостей (cv2/insightface) — юнит-тестируем в изоляции."""
import threading

from logging_utils import configure_logging

shutdown_event = threading.Event()
logger = configure_logging("facewatch.worker")


def handle_shutdown_signal(signum, _frame):
    logger.info("получен сигнал, начинаю graceful shutdown", extra={"signum": signum})
    shutdown_event.set()
