"""Логика хранения архива: retention по камерам и циклическая перезапись
(SPEC §5, §21).

Модуль `storage.py` — чистая логика на stdlib, поэтому здесь нет ни
`importorskip`, ни БД: проверка обязана выполняться в CI-джобе воркера,
которая ставит только `pytest fastapi httpx defusedxml` (урок цикла 16).

Позитивный контроль (урок цикла 21) — `test_camera_without_own_retention_
follows_global`: при «фиксе», который отбраковывает всё подряд, он падает
вместе с остальными, а при «фиксе», который не удаляет ничего, — остаётся
единственным зелёным.
"""
from datetime import datetime, timedelta

from storage import (bytes_to_free, days_left, disk_alert_level,
                     effective_retention_days, expired_segments,
                     nominal_gb_per_day, oldest_segments_to_free, required_gb)

NOW = datetime(2026, 8, 6, 12, 0, 0)


class Seg:
    """Минимальный сегмент: `storage` работает по утиной типизации, чтобы
    не тащить сюда ни модель воркера, ни SQLAlchemy."""

    def __init__(self, camera_id, days_ago, size_bytes=0):
        self.camera_id = camera_id
        self.started_at = NOW - timedelta(days=days_ago)
        self.size_bytes = size_bytes

    def __repr__(self):
        return f"Seg(cam={self.camera_id}, {(NOW - self.started_at).days}d)"


# --- retention: глобальный и по камерам (SPEC §5) -------------------------

def test_camera_without_own_retention_follows_global():
    assert effective_retention_days(1, 14, {}) == 14
    assert effective_retention_days(1, 14, {1: None}) == 14
    # Ключевое: смена глобальной настройки двигает и такие камеры.
    assert effective_retention_days(1, 30, {1: None}) == 30


def test_camera_with_own_retention_overrides_global():
    assert effective_retention_days(1, 14, {1: 3}) == 3
    assert effective_retention_days(1, 14, {1: 90}) == 90


def test_expired_uses_per_camera_depth_not_global():
    """Смысл фичи: две камеры с разной глубиной чистятся по-разному.

    Камера 1 хранит 3 дня, камера 2 — 30 при глобальных 14. Сегмент
    возрастом 7 дней просрочен у первой и жив у второй; до фикса обе
    сравнивались с одним глобальным cutoff и семидневный жил у обеих.
    """
    segs = [Seg(1, days_ago=7), Seg(2, days_ago=7)]
    expired = expired_segments(segs, NOW, global_days=14,
                               per_camera={1: 3, 2: 30})
    assert [s.camera_id for s in expired] == [1]


def test_camera_retention_longer_than_global_keeps_segment():
    """Обратное направление: собственный срок длиннее глобального защищает.

    Отдельный тест, потому что отбор кандидатов в `cleanup_old()` идёт по
    минимальному сроку, а решение — по каждой камере; ошибка в одной из
    двух половин ловится только с обеих сторон.
    """
    segs = [Seg(2, days_ago=20)]
    assert expired_segments(segs, NOW, global_days=14, per_camera={2: 30}) == []
    # Та же камера без собственного срока — сегмент просрочен.
    assert len(expired_segments(segs, NOW, global_days=14, per_camera={})) == 1


def test_expired_returns_same_objects_for_deletion():
    seg = Seg(1, days_ago=99)
    assert expired_segments([seg], NOW, 14, {})[0] is seg


# --- циклическая перезапись (SPEC §5, §21) --------------------------------

def test_no_eviction_while_free_space_above_target():
    assert bytes_to_free(total=1000, free=500, target_free_pct=5) == 0
    assert oldest_segments_to_free([Seg(1, 1, 100)], need_bytes=0) == []


def test_bytes_to_free_counts_up_to_target():
    # 5% от 1000 = 50; свободно 10 → освободить 40.
    assert bytes_to_free(total=1000, free=10, target_free_pct=5) == 40


def test_eviction_takes_oldest_first_and_stops_at_target():
    """Берётся ровно столько старейших, сколько покрывает нужду.

    Порядок задаёт вызывающий (`ORDER BY started_at`), и здесь он
    сохраняется: если бы функция пересортировала список, самый старый
    сегмент мог бы уцелеть, а свежий — уйти.
    """
    segs = [Seg(1, days_ago=d, size_bytes=100) for d in (10, 9, 8, 7)]
    victims = oldest_segments_to_free(segs, need_bytes=250)
    assert len(victims) == 3                       # 100+100+100 ≥ 250
    assert [s.started_at for s in victims] == [s.started_at for s in segs[:3]]


def test_eviction_ignores_retention_by_design():
    """Перезапись сносит и сегменты внутри срока хранения.

    Не побочный эффект, а требование SPEC §21 («автоудаление старейших
    сегментов при переполнении»): иначе на переполненном диске запись
    встала бы целиком до истечения retention.
    """
    fresh = Seg(1, days_ago=0, size_bytes=10 ** 9)
    assert oldest_segments_to_free([fresh], need_bytes=1) == [fresh]


def test_segments_with_unknown_size_are_still_deletable():
    """Строки до появления `size_bytes` (размер 0) не блокируют перезапись.

    Они не приближают цель по объёму, поэтому проход идёт дальше по списку
    и добирает реальные, — но и сами удаляются, иначе старый архив стал бы
    неудаляемым навсегда.
    """
    segs = [Seg(1, 10, size_bytes=0), Seg(1, 9, size_bytes=0), Seg(1, 8, size_bytes=500)]
    victims = oldest_segments_to_free(segs, need_bytes=400)
    assert len(victims) == 3
    assert victims[0].size_bytes == 0


def test_eviction_stops_at_end_when_archive_smaller_than_need():
    """Архив меньше, чем нужно освободить, — отдаётся всё, без зацикливания."""
    segs = [Seg(1, 5, 10), Seg(1, 4, 10)]
    assert len(oldest_segments_to_free(segs, need_bytes=10 ** 9)) == 2


def test_bytes_to_free_on_unreadable_disk_is_zero():
    """`shutil.disk_usage` на недоступном пути отдаёт нули — перезапись
    не должна на них сносить архив."""
    assert bytes_to_free(total=0, free=0, target_free_pct=5) == 0


# --- прогноз и алерты (SPEC §5, §14, §21) ---------------------------------

def test_days_left_is_none_until_consumption_measured():
    assert days_left(free_bytes=10 ** 12, bytes_per_day=0) is None


def test_days_left_divides_free_by_daily_consumption():
    assert days_left(free_bytes=1000, bytes_per_day=100) == 10.0


def test_disk_alert_thresholds_match_spec():
    # SPEC §14: «диск > 80%/90%».
    assert disk_alert_level(79.9) is None
    assert disk_alert_level(80.0) == "warning"
    assert disk_alert_level(89.9) == "warning"
    assert disk_alert_level(90.0) == "critical"


def test_spec_storage_formula():
    """SPEC §21: базовый расчёт приведён в самом ТЗ — сверяемся с ним.

    «на камеру: 21.6 GB/сутки», «120 камер: ~2.6 TB/сутки (номинал)»,
    «7 дней ≈ 18 TB; 14 дней ≈ 36 TB; 30 дней ≈ 78 TB» при 2 Mbps.
    """
    assert nominal_gb_per_day(2048) == 2.048 * 10.8
    assert round(nominal_gb_per_day(2000), 1) == 21.6
    assert round(nominal_gb_per_day(2000, 120) / 1024, 1) == 2.5   # ~2.6 ТБ
    assert round(required_gb(2000, 120, 7) / 1024) == 18
    assert round(required_gb(2000, 120, 14) / 1024) == 35          # ≈36
    assert round(required_gb(2000, 120, 30) / 1024) == 76          # ≈78
