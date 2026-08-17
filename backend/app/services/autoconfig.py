"""Автоконфигурация при первом запуске (SPEC §16).

SPEC §16: «Система определяет доступные ресурсы и предлагает: максимальное
количество камер записи, максимальное количество камер analytics,
рекомендуемый профиль производительности».

Модуль разделён на две половины по тому же принципу, что и `storage.py`:

* `detect_resources()` — единственное место, которое трогает железо
  (psutil, диск, признаки GPU). Не тестируется арифметически, зато и не
  содержит решений.
* `plan()` и функции под ней — чистая арифметика над числами из §16 и §19.
  Их можно проверить на любых входах, не имея ни 64 потоков, ни 40 ТБ
  массива, — и именно они решают, что показать администратору.

**Почему берётся верх каждого диапазона §16.** Расход в §16 задан вилками
(«0.02-0.04 ядра на камеру», «500 MB-2 GB на камеру»). Рекомендация,
посчитанная по нижней границе, обещает больше камер, чем сервер вывезет:
администратор заведёт их все, а обнаружит это через сутки по пропущенным
сегментам. По верхней границе система обещает меньше и ошибается в
сторону, которую видно сразу и которая ничего не ломает. Поэтому все
константы ниже — верхние края вилок §16, а не середины.

**Запрет §22 на хардкод числа камер.** Ни одно число здесь не является
количеством камер: количество получается делением измеренного ресурса на
удельный расход. На целевом сервере из §20 (64 потока, 128 ГБ, массив на
десятки ТБ) расчёт даёт около сотни камер записи не потому, что «120»
где-то записано, а потому, что столько влезает по диску.
"""
from __future__ import annotations

import os
import shutil

# --- Слой записи, SPEC §16 «Слой записи» --------------------------------
# «CPU: ~0.02-0.04 ядра на камеру (MediaMTX remux)» — берётся верх вилки.
REC_CORES_PER_CAMERA = 0.04
# «RAM: ~50-100 MB на камеру (буферы)» — верх вилки.
REC_RAM_MB_PER_CAMERA = 100

# --- Слой аналитики, SPEC §16 «Слой аналитики» --------------------------
# «MobileFaceNet CPU: ~0.5-1.5 ядра на камеру (5 FPS)» — верх вилки.
ANALYTICS_CORES_PER_CAMERA = 1.5
# «С GPU: ~0.1 ядра CPU + GPU на камеру».
ANALYTICS_CORES_PER_CAMERA_GPU = 0.1
# «RAM: ~500 MB-2 GB на камеру (модель + буферы)» — верх вилки.
ANALYTICS_RAM_MB_PER_CAMERA = 2048

# --- Запас, SPEC §19 «Общая производительность» -------------------------
# «CPU ≤ 80% при полной нагрузке (запас 20%)», «RAM ≤ 80% от доступной».
# Рекомендация, съедающая 100% ресурса, нарушает §19 по построению.
HEADROOM = 0.8

# --- Слой приложения, SPEC §2 -------------------------------------------
# FastAPI + React + PostgreSQL + Redis + Nginx работают на том же хосте и
# в вилки §16 (они про камеры) не входят. Без этого вычета обе цифры
# завышены на постоянную величину, и тем заметнее, чем слабее сервер.
APP_CORES = 2.0
APP_RAM_MB = 4096

# Границы настройки `analytics_cameras_max` (routers/settings.py:SCHEMA).
# Рекомендация обязана попадать в них, иначе «применить» упрётся в
# валидацию и администратор получит ошибку вместо настройки.
ANALYTICS_SETTING_MIN = 1
ANALYTICS_SETTING_MAX = 16

# Профиль (§15 «Профили производительности аналитики») выбирается по тому,
# сколько ядер приходится на одну камеру аналитики.
#
# Опора — та же вилка §16 «0.5-1.5 ядра на камеру (5 FPS)», снятая для
# лёгкой модели. `standard` — это она и есть (buffalo_s, префикс движения),
# поэтому его порог совпадает с верхом вилки. `maximum` гоняет детектор по
# КАЖДОМУ кадру, тяжёлую модель buffalo_l и кадр 960 px вместо 640 — по
# сумме множителей это втрое дороже, отсюда 3 ядра. `economy` (каждый 4-й
# кадр) дешевле стандартного примерно втрое и остаётся тем, что предлагают
# всему, что не дотянуло до `standard`.
PROFILE_CORES_MAXIMUM = 3.0
PROFILE_CORES_STANDARD = 1.5

# Названия ресурсов для текстов, которые уходят администратору как есть.
# Сам ключ (`bound_by`) остаётся английским: по нему интерфейс выбирает
# свою подпись, и переводить его в ответе значило бы ломать разбор.
RESOURCE_NAMES_RU = {"cpu": "CPU", "ram": "оперативную память", "disk": "диск"}

BYTES_PER_GB = 1024 ** 3
# SPEC §21: «Формула расчёта: Mbps × 10.8 = GB/сутки на камеру».
# Продублирована из services/storage.py сознательно — тот модуль про
# прогноз уже работающего архива, этот про планирование до первой камеры;
# расхождение стережёт тест, сверяющий обе копии с числом из SPEC.
GB_PER_DAY_PER_MBPS = 10.8


def gb_per_camera_day(bitrate_kbps: float) -> float:
    """Сколько ГБ в сутки занимает одна камера записи (SPEC §21)."""
    return max(0.0, bitrate_kbps) / 1000.0 * GB_PER_DAY_PER_MBPS


def _floor_nonneg(value: float) -> int:
    """Число камер: вниз до целого и никогда не отрицательное.

    Отрицательный бюджет — штатная ситуация, а не ошибка входа: слой
    приложения (`APP_CORES`) может не помещаться в машину целиком, и тогда
    разность отрицательна. Ответ на это — «ноль камер», а не «минус
    семнадцать», которое ушло бы в интерфейс числом.
    """
    if value <= 0:
        return 0
    return int(value)


def recording_capacity(cores: float, ram_mb: float, disk_free_gb: float,
                       bitrate_kbps: float, retention_days: int) -> dict:
    """Предел слоя записи по каждому ресурсу отдельно (SPEC §16).

    Возвращает лимит по CPU, RAM и диску и то, какой из них связывает.
    Одно итоговое число («можно 132 камеры») администратору бесполезно:
    оно не подсказывает, что менять. «132, упирается в диск» — подсказывает
    ровно одно действие и потому отдаётся вместе с числом.
    """
    per_day = gb_per_camera_day(bitrate_kbps)
    disk_per_camera = per_day * max(1, retention_days)

    by_cpu = _floor_nonneg(cores / REC_CORES_PER_CAMERA)
    by_ram = _floor_nonneg(ram_mb / REC_RAM_MB_PER_CAMERA)
    # Битрейт 0 означал бы «камера не пишет ничего»: предел по диску в этом
    # случае не определён, а не бесконечен, — поэтому 0, а не пропуск
    # ресурса из сравнения (пропуск отдал бы «сколько влезет по CPU»).
    by_disk = _floor_nonneg(disk_free_gb / disk_per_camera) if disk_per_camera > 0 else 0

    limits = {"cpu": by_cpu, "ram": by_ram, "disk": by_disk}
    cameras = min(limits.values())
    # Связывающим считается ресурс с минимальным пределом; при равенстве
    # порядок фиксирован (cpu, ram, disk), чтобы ответ не «плавал».
    bound_by = next(k for k in ("cpu", "ram", "disk") if limits[k] == cameras)
    return {
        "cameras": cameras,
        "by_cpu": by_cpu,
        "by_ram": by_ram,
        "by_disk": by_disk,
        "bound_by": bound_by,
        "gb_per_camera_day": round(per_day, 2),
        "gb_per_camera_retention": round(disk_per_camera, 1),
    }


def analytics_capacity(cores: float, ram_mb: float, gpu: bool = False) -> dict:
    """Предел слоя аналитики по CPU и RAM (SPEC §16).

    `cores`/`ram_mb` — то, что осталось ПОСЛЕ слоя приложения и записи:
    §2 требует независимости слоёв, а независимость означает, что
    аналитика не имеет права занять ядра, на которых стоит запись.
    """
    per_camera_cores = ANALYTICS_CORES_PER_CAMERA_GPU if gpu else ANALYTICS_CORES_PER_CAMERA
    by_cpu = _floor_nonneg(cores / per_camera_cores)
    by_ram = _floor_nonneg(ram_mb / ANALYTICS_RAM_MB_PER_CAMERA)
    cameras = min(by_cpu, by_ram)
    bound_by = "cpu" if by_cpu <= by_ram else "ram"
    return {
        "cameras": cameras,
        "by_cpu": by_cpu,
        "by_ram": by_ram,
        "bound_by": bound_by,
        "cores_per_camera": per_camera_cores,
    }


def recommend_profile(cores_available: float, analytics_cameras: int,
                      gpu: bool = False) -> str:
    """Рекомендуемый профиль производительности (SPEC §15, §16).

    Считается по фактическому запасу ядер на одну камеру аналитики, а не
    по марке процессора: две одинаковые по названию машины с разным числом
    камер аналитики требуют разных профилей.
    """
    if analytics_cameras <= 0:
        # Аналитику включать не на чем. Профиль всё равно нужно назвать —
        # он записан в настройках и применится, как только камера
        # появится, — и называется самый дешёвый.
        return "economy"
    if gpu:
        # §16: с GPU на камеру уходит ~0.1 ядра CPU, и потолок задаёт уже
        # не CPU. Тяжёлая модель на GPU — то, ради чего его ставят.
        return "maximum"
    per_camera = cores_available / analytics_cameras
    if per_camera >= PROFILE_CORES_MAXIMUM:
        return "maximum"
    if per_camera >= PROFILE_CORES_STANDARD:
        return "standard"
    return "economy"


def plan(cores: int, ram_mb: float, disk_free_gb: float, *,
         bitrate_kbps: float = 2048, retention_days: int = 14,
         gpu: bool = False) -> dict:
    """Полное предложение автоконфигурации (SPEC §16).

    Порядок вычета не произволен и определяется §2: слой записи — базовая
    функция NVR, отказ аналитики его не касается. Поэтому бюджет режется
    так: сначала слой приложения (без него не работает ничего), затем
    запись во весь предложенный ей размер, и лишь остаток достаётся
    аналитике.

    Резервировать под запись именно предложенный максимум (а не текущее
    число заведённых камер) — сознательно: на первом запуске камер ноль, и
    расчёт «от текущего» отдал бы аналитике весь сервер, а через неделю,
    когда камеры заведут, обещание перестало бы выполняться. Тихая
    деградация аналитики через неделю после настройки — ровно то, чего
    автоконфигурация должна избежать.
    """
    budget_cores = max(0.0, cores * HEADROOM - APP_CORES)
    budget_ram_mb = max(0.0, ram_mb * HEADROOM - APP_RAM_MB)

    rec = recording_capacity(budget_cores, budget_ram_mb, disk_free_gb,
                             bitrate_kbps, retention_days)

    # Остаток после записи — вход аналитики.
    left_cores = max(0.0, budget_cores - rec["cameras"] * REC_CORES_PER_CAMERA)
    left_ram_mb = max(0.0, budget_ram_mb - rec["cameras"] * REC_RAM_MB_PER_CAMERA)

    ana = analytics_capacity(left_cores, left_ram_mb, gpu)
    # Предложение не может выйти за границы настройки: рекомендация,
    # которую нельзя применить, — это не рекомендация.
    ana_setting = min(ana["cameras"], ANALYTICS_SETTING_MAX)

    profile = recommend_profile(left_cores, ana_setting, gpu)

    warnings: list[str] = []
    if ana_setting == 0:
        warnings.append(
            "Ресурсов не хватает даже на одну камеру аналитики с запасом §19. "
            f"Держите камеры в режиме record_only: на аналитику нужно "
            f"~{ANALYTICS_CORES_PER_CAMERA} ядра и "
            f"{ANALYTICS_RAM_MB_PER_CAMERA} МБ на камеру, свободно "
            f"{left_cores:.1f} ядра и {int(left_ram_mb)} МБ."
        )
    if ana["cameras"] > ANALYTICS_SETTING_MAX:
        warnings.append(
            f"По ресурсам проходит {ana['cameras']} камер аналитики, но "
            f"настройка ограничена {ANALYTICS_SETTING_MAX}: выше этого числа "
            "нужен GPU (§15)."
        )
    if rec["cameras"] == 0:
        warnings.append(
            "Ресурсов не хватает ни на одну камеру записи с запасом §19 — "
            # Название ресурса переводится здесь, а не оставляется ключом:
            # в браузере строка выходила как «упирается в disk» рядом с
            # «упирается в диск» в соседней карточке, которую переводит
            # интерфейс. Поймано просмотром страницы, а не тестом.
            f"упирается в {RESOURCE_NAMES_RU[rec['bound_by']]}."
        )

    return {
        "recording_max": rec["cameras"],
        "analytics_max": ana_setting,
        "analytics_by_resources": ana["cameras"],
        "profile": profile,
        "recording": rec,
        "analytics": ana,
        "budget": {
            "cores_total": cores,
            "cores_usable": round(cores * HEADROOM, 2),
            "cores_for_layers": round(budget_cores, 2),
            "cores_left_for_analytics": round(left_cores, 2),
            "ram_mb_total": int(ram_mb),
            "ram_mb_usable": int(ram_mb * HEADROOM),
            "ram_mb_for_layers": int(budget_ram_mb),
            "ram_mb_left_for_analytics": int(left_ram_mb),
            "headroom": HEADROOM,
            "app_cores": APP_CORES,
            "app_ram_mb": APP_RAM_MB,
        },
        "assumptions": {
            "bitrate_kbps": bitrate_kbps,
            "retention_days": retention_days,
            "gpu": gpu,
            "rec_cores_per_camera": REC_CORES_PER_CAMERA,
            "rec_ram_mb_per_camera": REC_RAM_MB_PER_CAMERA,
            "analytics_cores_per_camera": ana["cores_per_camera"],
            "analytics_ram_mb_per_camera": ANALYTICS_RAM_MB_PER_CAMERA,
        },
        "warnings": warnings,
    }


def detect_gpu() -> bool:
    """Есть ли на хосте ускоритель, пригодный для инференса (SPEC §16).

    Проба намеренно грубая и без импорта onnxruntime: бэкенд его не ставит
    (инференс живёт в воркере), а тянуть 200 МБ ради ответа «да/нет» на
    экране планирования незачем. Ошибка в сторону «нет» безопасна —
    рекомендация выйдет консервативнее фактических возможностей.
    """
    if shutil.which("nvidia-smi"):
        return True
    # Intel/AMD: узлы рендера появляются только при наличии iGPU/dGPU.
    return any(os.path.exists(p) for p in ("/dev/dri/renderD128", "/dev/dri/card0"))


def detect_resources(media_path: str) -> dict:
    """Фактические ресурсы хоста (SPEC §16 «определяет доступные ресурсы»).

    `disk_free_gb` берётся по разделу архива, а не по корню: на целевом
    сервере §20 архив лежит на отдельном массиве, и свободное место на
    системном диске к числу камер отношения не имеет.
    """
    import psutil

    logical = psutil.cpu_count(logical=True) or 1
    physical = psutil.cpu_count(logical=False) or logical
    mem = psutil.virtual_memory()
    try:
        disk = shutil.disk_usage(media_path)
        disk_total_gb = disk.total / BYTES_PER_GB
        disk_free_gb = disk.free / BYTES_PER_GB
    except OSError:
        # Каталог архива может быть ещё не смонтирован на первом запуске —
        # это не повод не показать экран целиком.
        disk_total_gb = disk_free_gb = 0.0
    return {
        "cores_logical": logical,
        "cores_physical": physical,
        "ram_mb": mem.total / (1024 ** 2),
        "disk_total_gb": round(disk_total_gb, 1),
        "disk_free_gb": round(disk_free_gb, 1),
        "gpu": detect_gpu(),
        "media_path": media_path,
    }
