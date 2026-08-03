"""Профили производительности (ТЗ 18.9).

Профиль задаёт связный набор параметров обработки. Любой параметр можно
переопределить вручную — профиль лишь заполняет значения по умолчанию.
Воркер читает итоговые значения из таблицы settings.
"""

PROFILES: dict[str, dict] = {
    # Intel N100 и подобные: 8 камер, CPU < 50%
    "economy": {
        "detection_fps": 5,
        "frame_skip": 3,          # анализировать каждый 4-й кадр
        "motion_prefilter": 1,    # детектор лиц только по движению
        "idle_fps": 1,            # частота при длительном отсутствии движения
        "face_model": "buffalo_s",
        "upscale_mode": "manual",  # апскейл только по запросу оператора
        "cluster_interval_min": 60,
        "detect_width": 640,
    },
    # Core i3 и выше: 12 камер, CPU < 60%
    "standard": {
        "detection_fps": 10,
        "frame_skip": 1,
        "motion_prefilter": 1,
        "idle_fps": 2,
        "face_model": "buffalo_s",
        "upscale_mode": "avatar",  # в фоне улучшается только аватар персоны
        "cluster_interval_min": 15,
        "detect_width": 640,
    },
    # Ryzen 5+/GPU: максимальное качество
    "maximum": {
        "detection_fps": 15,
        "frame_skip": 0,
        "motion_prefilter": 0,     # анализируется каждый кадр
        "idle_fps": 5,
        "face_model": "buffalo_l",
        "upscale_mode": "all",     # апскейл всей галереи в фоне
        "cluster_interval_min": 5,
        "detect_width": 960,
    },
}

DEFAULT_PROFILE = "standard"

# Ключи, которыми управляет профиль (их значения перезаписываются при его смене)
PROFILE_KEYS = tuple(PROFILES[DEFAULT_PROFILE].keys())


def profile_settings(name: str) -> dict[str, str]:
    """Значения профиля в виде строк для таблицы settings."""
    data = PROFILES.get(name) or PROFILES[DEFAULT_PROFILE]
    return {k: str(v) for k, v in data.items()}
