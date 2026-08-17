"""Расписание детекции через API камеры (SPEC §6) — production path на
реальном Postgres.

Оценка расписания проверяется в воркере
(`worker/tests/test_detection_schedule.py`); здесь — то, за что отвечает
бэкенд: расписание доезжает до колонки, возвращается в форму без потерь и
не принимает то, чего воркер не поймёт.

Отдельно проверяется, что **ночное окно (22:00–06:00) не отвергается
валидацией**. Правило «начало раньше конца» выглядит очевидной проверкой
корректности и запрещает ровно половину функции: §6 называет режим
«день/ночь» прямо.
"""
import pytest


NIGHT = {"enabled": True, "windows": [{"days": [0, 1, 2, 3, 4, 5, 6],
                                       "start": "22:00", "end": "06:00"}]}
WORKING_HOURS = {"enabled": True, "windows": [{"days": [0, 1, 2, 3, 4],
                                               "start": "08:00", "end": "18:00"}]}


def test_camera_without_schedule_stores_null(client, admin_headers, make_camera):
    """Расписания нет ни у одной существующей камеры, и NULL здесь означает
    «детекция круглосуточно» — см. models.Camera.detection_schedule."""
    cam = make_camera("sched-none")
    assert cam["detection_schedule"] is None


def test_schedule_survives_round_trip(client, admin_headers, make_camera):
    """Форма редактирования обязана вернуть расписание обратно без потерь:
    иначе сохранение любой другой правки камеры стирало бы его — ровно та
    ошибка, что уже случалась с ONVIF-полями."""
    cam = make_camera("sched-roundtrip", mode="analytics", detection_schedule=WORKING_HOURS)
    assert cam["detection_schedule"] == WORKING_HOURS

    listed = client.get("/api/cameras", headers=admin_headers).json()
    stored = next(c for c in listed if c["id"] == cam["id"])
    assert stored["detection_schedule"] == WORKING_HOURS


def test_night_window_is_accepted(client, admin_headers, make_camera):
    """22:00–06:00 — «ночная охрана», а не опечатка. Проверка «начало раньше
    конца» запретила бы половину §6."""
    cam = make_camera("sched-night", mode="analytics", detection_schedule=NIGHT)
    assert cam["detection_schedule"]["windows"][0]["start"] == "22:00"
    assert cam["detection_schedule"]["windows"][0]["end"] == "06:00"


def test_schedule_can_be_cleared(client, admin_headers, make_camera):
    """Пустое значение значимо: «снять расписание, вернуть круглосуточную
    детекцию». Без этого снять расписание было бы нечем."""
    cam = make_camera("sched-clear", mode="analytics", detection_schedule=NIGHT)
    r = client.put(f"/api/cameras/{cam['id']}",
                   json={"name": "sched-clear", "rtsp_url": "rtsp://cam/x",
                         "mode": "analytics", "detection_schedule": None},
                   headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["detection_schedule"] is None


def test_schedule_can_be_replaced(client, admin_headers, make_camera):
    cam = make_camera("sched-replace", mode="analytics", detection_schedule=NIGHT)
    r = client.put(f"/api/cameras/{cam['id']}",
                   json={"name": "sched-replace", "rtsp_url": "rtsp://cam/x",
                         "mode": "analytics", "detection_schedule": WORKING_HOURS},
                   headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["detection_schedule"] == WORKING_HOURS


@pytest.mark.parametrize("windows", [
    [{"days": [0], "start": "8:00", "end": "18:00"}],       # без ведущего нуля
    [{"days": [0], "start": "25:00", "end": "18:00"}],      # часов не бывает
    [{"days": [0], "start": "08:70", "end": "18:00"}],      # минут не бывает
    [{"days": [0], "start": "08:00"}],                       # нет конца
    [{"days": [7], "start": "08:00", "end": "18:00"}],      # дня недели 7 не бывает
    [{"days": [-1], "start": "08:00", "end": "18:00"}],
    [{"days": ["пн"], "start": "08:00", "end": "18:00"}],
])
def test_invalid_window_is_rejected(client, admin_headers, windows):
    """Форма всегда шлёт `HH:MM`; всё остальное отвергается здесь, а не
    оседает в колонке, которую разбирает цикл кадров воркера."""
    r = client.post("/api/cameras",
                    json={"name": "sched-bad", "rtsp_url": "rtsp://cam/bad",
                          "mode": "analytics",
                          "detection_schedule": {"enabled": True, "windows": windows}},
                    headers=admin_headers)
    assert r.status_code == 422, r.text


def test_too_many_windows_rejected(client, admin_headers):
    """Потолок совпадает с MAX_WINDOWS воркера: окна разбираются в цикле
    кадров, и колонка не должна становиться способом его занять."""
    windows = [{"days": [0], "start": "08:00", "end": "09:00"} for _ in range(11)]
    r = client.post("/api/cameras",
                    json={"name": "sched-many", "rtsp_url": "rtsp://cam/many",
                          "mode": "analytics",
                          "detection_schedule": {"enabled": True, "windows": windows}},
                    headers=admin_headers)
    assert r.status_code == 422


def test_exactly_ten_windows_accepted(client, admin_headers, make_camera):
    """Обратная сторона потолка: сама граница — законное значение."""
    windows = [{"days": [0], "start": "08:00", "end": "09:00"} for _ in range(10)]
    cam = make_camera("sched-ten", mode="analytics",
                      detection_schedule={"enabled": True, "windows": windows})
    assert len(cam["detection_schedule"]["windows"]) == 10


def test_window_without_days_defaults_to_every_day(client, admin_headers, make_camera):
    """Так задаётся «ночь всегда», не перечисляя все семь дней."""
    cam = make_camera("sched-nodays", mode="analytics",
                      detection_schedule={"enabled": True,
                                          "windows": [{"start": "22:00", "end": "06:00"}]})
    assert cam["detection_schedule"]["windows"][0]["days"] == [0, 1, 2, 3, 4, 5, 6]


def test_duplicate_days_are_collapsed(client, admin_headers, make_camera):
    cam = make_camera("sched-dupes", mode="analytics",
                      detection_schedule={"enabled": True,
                                          "windows": [{"days": [0, 0, 1], "start": "08:00", "end": "18:00"}]})
    assert cam["detection_schedule"]["windows"][0]["days"] == [0, 1]


def test_schedule_requires_admin(client, make_user_headers, make_camera):
    """Расписание — часть управления камерами (§18: только администратор)."""
    cam = make_camera("sched-rbac", mode="analytics")
    headers = make_user_headers("sched-operator", "operator")
    r = client.put(f"/api/cameras/{cam['id']}",
                   json={"name": "sched-rbac", "rtsp_url": "rtsp://cam/x",
                         "mode": "analytics", "detection_schedule": NIGHT},
                   headers=headers)
    assert r.status_code == 403
