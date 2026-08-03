"""Экспоненциальный бэкофф переподключения к RTSP (ТЗ 12: "автопереподключение
с экспоненциальной задержкой"). Без него потеря камеры на несколько часов
означает попытку открыть RTSP-соединение раз в 2 секунды бесконечно.

Вынесено в отдельный модуль без тяжёлых зависимостей (cv2/insightface),
чтобы логику можно было юнит-тестировать без установки ML-стека воркера.
"""

RECONNECT_BASE_DELAY_SEC = 2.0
RECONNECT_MAX_DELAY_SEC = 60.0


def reconnect_delay(attempt: int, base: float = RECONNECT_BASE_DELAY_SEC,
                     max_delay: float = RECONNECT_MAX_DELAY_SEC) -> float:
    """Задержка перед attempt-й (считая с 0) повторной попыткой подключения:
    2с, 4с, 8с, 16с, 32с, дальше не растёт — capped на max_delay."""
    return min(base * (2 ** max(0, attempt)), max_delay)
