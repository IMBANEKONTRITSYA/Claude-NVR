"""Координация graceful shutdown воркера (ТЗ 12). Отдельный модуль без
тяжёлых зависимостей (cv2/insightface) — юнит-тестируем в изоляции."""
import threading

shutdown_event = threading.Event()


def handle_shutdown_signal(signum, _frame):
    print(f"[worker] получен сигнал {signum}, начинаю graceful shutdown", flush=True)
    shutdown_event.set()
