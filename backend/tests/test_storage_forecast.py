"""Хранилище архива: заполнение, расход, прогноз, калькулятор (SPEC §5, §21).

Production path целиком: настоящий Postgres → `/api/system/storage`, с
засеянными сегментами вместо мока `func.sum`. Прогноз — единственное место
системы, где число из БД превращается в обещание администратору («хватит
на N дней»), и проверять его на моке значило бы проверять мок.

Отдельно стережётся дублирование формул между `backend/app/services/
storage.py` и `worker/storage.py`: у сервисов разные контексты сборки и
общего пакета нет, поэтому копии неизбежны, но разъехаться они не должны.
"""
import os
from datetime import datetime, timedelta

import pytest

from app.services.storage import (calibration, days_left, disk_alert_level,
                                  nominal_gb_per_day, required_gb)

GB = 1024 ** 3


@pytest.fixture()
def seed_segments(pg_conn, make_camera):
    """Засевает сегменты с известным размером и убирает их за собой."""
    created = []

    def _seed(camera_id: int, sizes_bytes, hours_ago_start: float = 1.0):
        with pg_conn.cursor() as cur:
            for i, size in enumerate(sizes_bytes):
                started = datetime.utcnow() - timedelta(hours=hours_ago_start + i * 0.01)
                cur.execute(
                    "INSERT INTO video_segments (camera_id, started_at, ended_at, "
                    "file_path, event_type, duration_sec, size_bytes) "
                    "VALUES (%s, %s, %s, %s, 'continuous', 300, %s) RETURNING id",
                    (camera_id, started, started + timedelta(minutes=5),
                     f"/media/segments/cam{camera_id}_{i}_test.mp4", size),
                )
                created.append(cur.fetchone()[0])
        return created

    yield _seed

    if created:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM video_segments WHERE id = ANY(%s)", (created,))


# --- формулы SPEC §21 -----------------------------------------------------

def test_backend_and_worker_formulas_agree():
    """Две копии формул не должны разъехаться (см. docstring модуля).

    Сверяются не друг с другом, а обе — с числами, выписанными в SPEC §21:
    сравнение копий между собой прошло бы и на двух одинаково неверных.
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "worker"))
    import storage as worker_storage

    for kbps, cams, days in ((2000, 1, 1), (2048, 120, 14), (256, 7, 30)):
        assert nominal_gb_per_day(kbps, cams) == worker_storage.nominal_gb_per_day(kbps, cams)
        assert required_gb(kbps, cams, days) == worker_storage.required_gb(kbps, cams, days)
    for pct in (0, 79.9, 80, 89.9, 90, 100):
        assert disk_alert_level(pct) == worker_storage.disk_alert_level(pct)
    # И обе — с ТЗ: «на камеру: 21.6 GB/сутки» при 2 Mbps.
    assert round(nominal_gb_per_day(2000), 1) == 21.6


def test_calibration_is_none_without_cameras():
    """Ноль включённых камер — прочерк, а не «расход в 0 раз больше»."""
    assert calibration(measured_gb_per_day=5.0, nominal_gb_per_day_value=0) is None


def test_calibration_ratio_reflects_vbr_saving():
    """SPEC §21: фактически ~1.6–2 ТБ/сутки против 2.6 ТБ номинала."""
    assert calibration(1800, 2600) == pytest.approx(0.69, abs=0.01)


# --- /api/system/storage --------------------------------------------------

def test_storage_report_shape_and_disk_numbers(client, admin_headers):
    r = client.get("/api/system/storage", headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    for key in ("disk_total_gb", "disk_used_gb", "disk_free_gb", "disk_used_percent",
                "alert_level", "archive_gb", "measured_gb_per_day",
                "nominal_gb_per_day", "days_left", "forecast_source",
                "cameras_recording", "retention_days", "per_camera_retention"):
        assert key in d, f"нет поля {key}"
    assert 0 <= d["disk_used_percent"] <= 100


def test_forecast_uses_measured_consumption_when_available(client, admin_headers,
                                                           make_camera, seed_segments):
    """Прогноз считается по фактическому расходу, а не по номиналу.

    Смысловая проверка фичи: 10 ГБ за последние сутки при свободных
    `disk_free_gb` дают ровно `disk_free_gb / 10` дней. Если бы прогноз
    брался с номинала (2 Mbps × 10.8 = 21.6 ГБ/сутки на камеру), число
    было бы примерно вдвое меньше.
    """
    cam = make_camera("storage-measured")
    seed_segments(cam["id"], [5 * GB, 5 * GB])

    d = client.get("/api/system/storage", headers=admin_headers).json()
    assert d["forecast_source"] == "measured"
    assert d["measured_gb_per_day"] == pytest.approx(10.0, abs=0.2)
    expected = d["disk_free_gb"] / d["measured_gb_per_day"]
    assert d["days_left"] == pytest.approx(expected, rel=0.02)


def test_old_segments_do_not_count_toward_daily_consumption(client, admin_headers,
                                                            make_camera, seed_segments):
    """Расход — за последние сутки, а не за всё время.

    Без окна прогноз на заполненном архиве считался бы по всему объёму
    записи и уезжал бы в разы; ловится только сегментом заведомо старше
    суток, который в `measured` попасть не должен.
    """
    cam = make_camera("storage-window")
    seed_segments(cam["id"], [3 * GB], hours_ago_start=1)
    fresh = client.get("/api/system/storage", headers=admin_headers).json()
    seed_segments(cam["id"], [50 * GB], hours_ago_start=48)
    after = client.get("/api/system/storage", headers=admin_headers).json()

    assert after["measured_gb_per_day"] == pytest.approx(fresh["measured_gb_per_day"], abs=0.1)
    # Но в общий объём архива старый сегмент входит.
    assert after["archive_gb"] > fresh["archive_gb"] + 40


def test_forecast_falls_back_to_nominal_without_measurements(client, admin_headers):
    """Первые сутки после развёртывания: расчётная оценка, а не прочерк."""
    d = client.get("/api/system/storage", headers=admin_headers).json()
    if d["measured_gb_per_day"] == 0:
        assert d["forecast_source"] == "nominal"
        if d["cameras_recording"]:
            assert d["days_left"] is not None


def test_per_camera_retention_is_reported(client, admin_headers, make_camera):
    """Камеры с собственной глубиной видны администратору списком."""
    cam = make_camera("storage-retention", retention_days=3)
    d = client.get("/api/system/storage", headers=admin_headers).json()
    assert d["per_camera_retention"].get(str(cam["id"])) == 3


def test_camera_without_own_retention_absent_from_overrides(client, admin_headers,
                                                            make_camera):
    """Позитивный контроль к предыдущему: NULL — не отклонение."""
    cam = make_camera("storage-retention-default")
    d = client.get("/api/system/storage", headers=admin_headers).json()
    assert str(cam["id"]) not in d["per_camera_retention"]


def test_storage_denied_to_viewer(client, make_user_headers):
    """Матрица прав SPEC §25: «Настройка retention и хранения» — админ;
    оператору отчёт доступен как часть системного мониторинга
    («Ограниченно»), наблюдателю — нет."""
    r = client.get("/api/system/storage", headers=make_user_headers("storage-viewer", "viewer"))
    assert r.status_code == 403


# --- калькулятор SPEC §21 -------------------------------------------------

def test_calculator_matches_spec_reference_numbers(client, admin_headers):
    """ТЗ §21 приводит контрольные числа — на них и сверяемся."""
    r = client.get("/api/system/storage/calculator",
                   params={"bitrate_kbps": 2000, "cameras": 120, "days": 14},
                   headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["gb_per_day_per_camera"] == pytest.approx(21.6, abs=0.1)
    assert d["required_tb"] == pytest.approx(35.6, abs=0.5)      # ТЗ: «14 дней ≈ 36 TB»


def test_calculator_rejects_out_of_range_params(client, admin_headers):
    """Границы numeric-параметров (приём цикла 21): без них `days=999999999`
    даёт бессмысленный ответ вместо отказа."""
    for params in ({"days": 999999999}, {"days": 0}, {"cameras": -1},
                   {"bitrate_kbps": 0}, {"cameras": 100000}):
        r = client.get("/api/system/storage/calculator", params=params, headers=admin_headers)
        assert r.status_code == 422, f"{params} → {r.status_code}"


def test_calculator_is_admin_only(client, make_user_headers):
    """SPEC §25: «Настройка retention и хранения» — только администратор."""
    r = client.get("/api/system/storage/calculator",
                   headers=make_user_headers("calc-operator", "operator"))
    assert r.status_code == 403


# --- умолчания калькулятора берутся из системы, а не из константы ---------
#
# §22 «Запрещено: хардкодить количество камер», §16 «формулы и калькуляторы
# вместо фиксированных чисел». До цикла 38 в сигнатуре стояло
# `cameras: int = Query(120)`, и у администратора объекта на 32 камеры
# калькулятор при каждом открытии считал объём для 120 — заведомо неверный
# ответ до того, как человек что-то ввёл.


def test_calculator_defaults_to_actual_camera_count(client, admin_headers, make_camera):
    """Без параметра `cameras` считается фактическое число включённых камер."""
    make_camera("calc-default-1")
    make_camera("calc-default-2")
    make_camera("calc-default-3")

    d = client.get("/api/system/storage/calculator", headers=admin_headers).json()
    enabled = client.get("/api/system/storage", headers=admin_headers).json()["cameras_recording"]

    assert d["cameras"] == enabled, "калькулятор обязан считать для этой системы"
    assert d["cameras"] != 120 or enabled == 120, "число 120 больше не подставляется"
    assert d["cameras_source"] == "actual"


def test_calculator_defaults_to_configured_retention(client, admin_headers):
    """Без параметра `days` берётся настроенный retention, а не константа 14.

    Исходное значение считывается, а не предполагается: в песочнице сид даёт
    14, в CI — 30 (`RETENTION_DAYS` окружения), и восстановление в
    захардкоженные 14 оставляло бы после теста чужую настройку. Ровно эту
    ошибку — подставленное вместо прочитанного — и чинит этот PR, так что
    допускать её в собственном тесте тем более нельзя.
    """
    # `/api/settings` отдаёт значения строками (таблица `settings` хранит
    # текст) — приводим явно, иначе `before + 7` склеит строки.
    before = int(client.get("/api/settings", headers=admin_headers).json()["retention_days"])
    probe = before + 7          # заведомо отличается от исходного
    client.put("/api/settings", json={"retention_days": probe}, headers=admin_headers)
    try:
        d = client.get("/api/system/storage/calculator", headers=admin_headers).json()
        assert d["days"] == probe
        assert d["days_source"] == "actual"
    finally:
        client.put("/api/settings", json={"retention_days": before}, headers=admin_headers)


def test_explicit_params_still_win(client, admin_headers, make_camera):
    """Позитивный контроль: сценарий «а что если» обязан работать как раньше.

    Без него «фикс», намертво прибивающий калькулятор к текущему состоянию
    системы, прошёл бы оба теста выше — и убил бы весь смысл калькулятора,
    который в том и состоит, чтобы считать для ещё не существующего объёма.
    """
    make_camera("calc-explicit")
    d = client.get("/api/system/storage/calculator",
                   params={"cameras": 250, "days": 30, "bitrate_kbps": 2000},
                   headers=admin_headers).json()
    assert d["cameras"] == 250 and d["days"] == 30
    assert d["cameras_source"] == "requested" and d["days_source"] == "requested"
    # И число действительно посчитано по введённым параметрам, а не по факту.
    assert d["gb_per_day_total"] == pytest.approx(21.6 * 250, rel=0.01)


def test_calculator_on_empty_system_does_not_return_zero(client, admin_headers):
    """Пустая система: ноль камер — это отсутствие данных, а не ответ «0 ГБ».

    Тест идёт на базе, где камеры могли остаться от соседних тестов,
    поэтому проверяется свойство, а не конкретное число: расчёт не должен
    вырождаться в ноль ни при каком состоянии БД.
    """
    d = client.get("/api/system/storage/calculator", headers=admin_headers).json()
    assert d["cameras"] >= 1
    assert d["required_gb"] > 0
    if d["cameras_source"] == "fallback_empty":
        assert d["cameras"] == 1


# --- retention по камере через API камер (SPEC §5) ------------------------

def test_camera_retention_roundtrip(client, admin_headers, make_camera):
    cam = make_camera("retention-roundtrip", retention_days=7)
    assert cam["retention_days"] == 7
    got = client.get("/api/cameras", headers=admin_headers).json()
    assert next(c for c in got if c["id"] == cam["id"])["retention_days"] == 7


def test_camera_retention_can_be_cleared_back_to_global(client, admin_headers, make_camera):
    """Снятие собственного срока — то самое поведение, ради которого PUT
    трактует отсутствующее поле как «сбросить», см. cameras.py."""
    cam = make_camera("retention-clear", retention_days=7)
    r = client.put(f"/api/cameras/{cam['id']}",
                   json={"name": cam["name"], "rtsp_url": "rtsp://cam/x"},
                   headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["retention_days"] is None


def test_camera_retention_bounds_are_enforced(client, admin_headers, make_camera):
    for bad in (0, -5, 4000):
        r = client.post("/api/cameras",
                        json={"name": f"ret-bad-{bad}", "rtsp_url": "rtsp://cam/x",
                              "retention_days": bad},
                        headers=admin_headers)
        assert r.status_code == 422, f"retention_days={bad} → {r.status_code}"
        if r.status_code == 200:
            make_camera.adopt(r.json()["id"])
