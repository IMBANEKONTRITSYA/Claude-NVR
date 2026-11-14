"""Управление дисковым пространством архива (SPEC §5, §21).

Чистая логика, без обращений к БД и файловой системе: решения «какие
сегменты просрочены» и «какие удалить, чтобы освободить место» принимаются
над списками простых структур и поэтому проверяются тестами напрямую, без
поднятия Postgres и без записи гигабайтов на диск.

Два независимых механизма удаления, которые SPEC §5 и §21 называют
раздельно и которые нельзя смешивать:

* **retention** — «настраиваемая глубина хранения (глобально и по камерам)»:
  сегмент старше своего срока удаляется независимо от того, сколько на
  диске места;
* **циклическая перезапись** — «автоудаление старейших сегментов при
  переполнении»: когда места меньше порога, удаляются самые старые сегменты
  по всему архиву, даже если их retention ещё не истёк.

Второй механизм — аварийный: он срабатывает, когда расчёт хранения не
сошёлся с фактическим битрейтом, и его задача не дать записи встать
целиком. Поэтому он идёт по всему архиву по времени, а не по камерам: на
120 камерах выбор «чью запись жертвовать» иначе пришлось бы принимать
здесь, а это решение администратора — он его уже выразил через retention.
"""
from __future__ import annotations

from datetime import datetime, timedelta

# Байт в гигабайте — везде считаем в двоичных ГБ (как shutil.disk_usage и
# как показывает ОС), чтобы цифра в интерфейсе сходилась с `df -h`.
BYTES_PER_GB = 1024 ** 3

# SPEC §21: «Формула расчёта: Mbps × 10.8 = GB/сутки на камеру».
GB_PER_DAY_PER_MBPS = 10.8

# SPEC §14: «Алерты: диск > 80%/90%».
DISK_WARN_PCT = 80.0
DISK_CRIT_PCT = 90.0


def effective_retention_days(camera_id: int, global_days: int,
                             per_camera: dict[int, int | None] | None) -> int:
    """Срок хранения для конкретной камеры (SPEC §5: глобально и по камерам).

    `None` в `per_camera` — не «ноль дней», а «значение не задано»: колонка
    `cameras.retention_days` nullable именно для того, чтобы камера по
    умолчанию следовала за глобальной настройкой и продолжала следовать за
    ней после её изменения. Ноль как «хранить вечно» здесь не вводится —
    SPEC §5 требует циклической перезаписи, а не бесконечного роста.
    """
    if not per_camera:
        return global_days
    own = per_camera.get(camera_id)
    return global_days if own is None else own


def expired_segments(segments, now: datetime, global_days: int,
                     per_camera: dict[int, int | None] | None = None) -> list:
    """Сегменты, вышедшие за свой срок хранения.

    `segments` — любые объекты с `camera_id` и `started_at`; возвращаются
    те же объекты, а не копии, чтобы вызывающий мог сразу удалить строки.

    Сравнение идёт по `started_at`, а не по `ended_at`: сегмент длиной до
    10 минут, и разница между ними меньше, чем шаг любой разумной ротации,
    зато `started_at` уже проиндексирован (`VideoSegment.started_at`).
    """
    per_camera = per_camera or {}
    out = []
    for seg in segments:
        days = effective_retention_days(seg.camera_id, global_days, per_camera)
        if seg.started_at < now - timedelta(days=days):
            out.append(seg)
    return out


def bytes_to_free(total: int, free: int, target_free_pct: float) -> int:
    """Сколько байт нужно освободить, чтобы свободного стало `target_free_pct`.

    Ноль (а не отрицательное число), когда места и так достаточно, — чтобы
    вызывающему хватало проверки `if need:` без сравнения с нулём.
    """
    if total <= 0:
        return 0
    want = total * target_free_pct / 100.0
    return int(max(0.0, want - free))


def oldest_segments_to_free(segments, need_bytes: int) -> list:
    """Старейшие сегменты, суммарно покрывающие `need_bytes`.

    `segments` должны идти от старых к новым — порядок задаёт вызывающий
    запросом с `ORDER BY started_at`, здесь он не переустанавливается:
    сортировать 100 000 строк в Python ради того, что БД уже сделала по
    индексу, значит удвоить работу на каждом проходе ротации.

    Сегменты с неизвестным размером (`size_bytes` = 0 у строк, записанных
    до появления колонки) считаются пустыми и не приближают цель, но
    удаляются наравне: иначе старый архив, у которого размеров нет, стал бы
    неудаляемым и заблокировал бы циклическую перезапись целиком.
    """
    if need_bytes <= 0:
        return []
    freed = 0
    out = []
    for seg in segments:
        out.append(seg)
        freed += max(0, getattr(seg, "size_bytes", 0) or 0)
        if freed >= need_bytes:
            break
    return out


def nominal_gb_per_day(bitrate_kbps: float, cameras: int = 1) -> float:
    """Номинальный расход по формуле SPEC §21 (Mbps × 10.8 × камеры)."""
    return max(0.0, bitrate_kbps) / 1000.0 * GB_PER_DAY_PER_MBPS * max(0, cameras)


def required_gb(bitrate_kbps: float, cameras: int, days: int) -> float:
    """Калькулятор хранения SPEC §21: битрейт × камеры × дни → объём."""
    return nominal_gb_per_day(bitrate_kbps, cameras) * max(0, days)


def days_left(free_bytes: int, bytes_per_day: float) -> float | None:
    """Прогноз «на сколько дней хватит места» (SPEC §5).

    `None`, когда расход ещё не измерен: показать «∞ дней» на нулевом
    расходе значило бы уверенно соврать в первые минуты после старта, пока
    ни один сегмент не дописан.
    """
    if bytes_per_day <= 0:
        return None
    return max(0.0, free_bytes / bytes_per_day)


def disk_alert_level(used_pct: float, warn_pct: float = DISK_WARN_PCT,
                     crit_pct: float = DISK_CRIT_PCT) -> str | None:
    """`None` | `"warning"` | `"critical"` по заполнению диска (SPEC §14)."""
    if used_pct >= crit_pct:
        return "critical"
    if used_pct >= warn_pct:
        return "warning"
    return None
