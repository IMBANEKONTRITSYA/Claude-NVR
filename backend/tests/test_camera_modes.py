"""Режим камеры record_only / analytics (SPEC §2, §3, §6, §11, §24) — цикл 24.

Ключевое решение Scale Edition: запись и аналитика — независимые слои, и
камера явно относится к одному из двух режимов. Запись идёт по всем
включённым камерам, распознавание — только по камерам `analytics`, которых
по умолчанию две из 120 (§1); §24 прямо выносит «Детекция лиц на всех 120
камерах без GPU» за рамки версии.

Тесты идут по production path: настоящий Postgres через общую фикстуру
`client`, настоящие обработчики роутеров, проверка результата чтением через
API — а не мок на `db.get`.
"""


def _reject_camera(client, headers, name: str, make_camera=None, **extra):
    """POST, от которого ожидается отказ. Возвращает ответ.

    Если камера всё-таки создалась (тест упал по существу или фикс
    откачен), она регистрируется на уборку — иначе провалившийся тест
    оставляет камеру в БД и роняет следующий, а не себя. Поймано в цикле 24
    ровно так: верификация фикса откатом наплодила камер, и упал сторож
    остатков в другом файле.
    """
    payload = {"name": name, "rtsp_url": f"rtsp://cam/{name}", **extra}
    r = client.post("/api/cameras", json=payload, headers=headers)
    if r.status_code == 200 and make_camera is not None:
        make_camera.adopt(r.json()["id"])
    return r


def _analytics_now(client, headers) -> int:
    """Сколько камер в режиме analytics прямо сейчас.

    Пределы в тестах ниже задаются **относительно** этого числа, а не
    абсолютом: тесты бэкенда идут по общей БД, и соседние файлы заводят свои
    камеры. Абсолютный предел делал бы результат зависящим от того, что
    успело выполниться раньше (тот же класс, что цикл 21 закрыл фикстурой
    make_user).
    """
    cams = client.get("/api/cameras", headers=headers).json()
    return sum(1 for c in cams if c.get("mode") == "analytics")


def _set_limit(client, headers, value: int):
    r = client.put("/api/settings", json={"analytics_cameras_max": value}, headers=headers)
    assert r.status_code == 200, r.text


def test_new_camera_defaults_to_record_only(client, admin_headers, make_camera):
    """SPEC §3: «режим (record_only/analytics)», по умолчанию record_only.

    Дефолт важен именно как дефолт: при массовом заведении камер аналитика
    не должна включаться сама.
    """
    cam = make_camera("default-mode")
    assert cam["mode"] == "record_only"
    listed = {c["id"]: c for c in client.get("/api/cameras", headers=admin_headers).json()}
    assert listed[cam["id"]]["mode"] == "record_only"


def test_camera_can_be_switched_between_modes(client, admin_headers, make_camera, restore_settings):
    """SPEC §2: «Переключение режима — через веб-интерфейс, без перезапуска
    слоёв»."""
    cam = make_camera("switch-mode")
    _set_limit(client, admin_headers, min(16, _analytics_now(client, admin_headers) + 1))
    r = client.put(
        f"/api/cameras/{cam['id']}",
        json={"name": "switch-mode", "rtsp_url": "rtsp://cam/switch-mode", "mode": "analytics"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["mode"] == "analytics"

    r = client.put(
        f"/api/cameras/{cam['id']}",
        json={"name": "switch-mode", "rtsp_url": "rtsp://cam/switch-mode", "mode": "record_only"},
        headers=admin_headers,
    )
    assert r.status_code == 200 and r.json()["mode"] == "record_only"


def test_unknown_mode_is_rejected(client, admin_headers, make_camera):
    r = _reject_camera(client, admin_headers, "bad-mode", make_camera, mode="everything")
    assert r.status_code == 422


def test_analytics_camera_count_is_capped(client, admin_headers, make_camera, restore_settings):
    """SPEC §1: аналитика — «только на N выбранных камерах (по умолчанию 2)».

    Без предела администратор включает аналитику на всех камерах, слой
    записи это переживает (он независим, §2), а воркер уходит в отставание —
    и выглядит это как «система тормозит», а не как «включено больше, чем
    рассчитано».
    """
    base = _analytics_now(client, admin_headers)
    _set_limit(client, admin_headers, min(16, base + 2))
    for i in range(2):
        make_camera(f"cap-ok-{i}", mode="analytics")

    r = _reject_camera(client, admin_headers, "cap-over", make_camera, mode="analytics")
    assert r.status_code == 400, r.text
    assert "предел" in r.json()["detail"]

    # Та же камера в режиме записи заводится без вопросов — предел относится
    # только к аналитике.
    assert make_camera("cap-over")["mode"] == "record_only"


def test_saving_analytics_camera_again_does_not_consume_a_slot(client, admin_headers, make_camera, restore_settings):
    """Редактирование камеры, которая уже в analytics, не должно упираться в
    предел из-за самой себя: иначе при пределе 1 её нельзя было бы даже
    переименовать."""
    base = _analytics_now(client, admin_headers)
    _set_limit(client, admin_headers, min(16, base + 1))
    cam = make_camera("self-slot", mode="analytics")
    # Слот исчерпан: любая другая камера в analytics теперь не заводится...
    assert _reject_camera(client, admin_headers, "self-slot-rival", make_camera,
                          mode="analytics").status_code == 400

    # ...а сама камера сохраняется, не упираясь в предел из-за себя же.
    r = client.put(
        f"/api/cameras/{cam['id']}",
        json={"name": "self-slot-renamed", "rtsp_url": "rtsp://cam/self-slot",
              "mode": "analytics"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "self-slot-renamed"


def test_roi_is_rejected_for_record_only_camera(client, admin_headers, make_camera, restore_settings):
    """SPEC §11: «доступно только для камер в режиме analytics».

    Сохранённые зоны детекции у камеры, которая только пишется, ни на что не
    влияют — принять их значит показать оператору настройку, которая молча
    ничего не делает.
    """
    cam = make_camera("roi-record-only")
    _set_limit(client, admin_headers, min(16, _analytics_now(client, admin_headers) + 1))
    r = client.put(
        f"/api/cameras/{cam['id']}/roi",
        json={"polygons": [[[0, 0], [10, 0], [10, 10]]]},
        headers=admin_headers,
    )
    assert r.status_code == 400, r.text

    client.put(
        f"/api/cameras/{cam['id']}",
        json={"name": "roi-record-only", "rtsp_url": "rtsp://cam/roi-record-only",
              "mode": "analytics"},
        headers=admin_headers,
    )
    r = client.put(
        f"/api/cameras/{cam['id']}/roi",
        json={"polygons": [[[0, 0], [10, 0], [10, 10]]]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert client.get(f"/api/cameras/{cam['id']}/roi", headers=admin_headers).json()["polygons"]


def test_analytics_limit_setting_bounds(client, admin_headers):
    before = client.get("/api/settings", headers=admin_headers).json()["analytics_cameras_max"]
    try:
        assert client.put("/api/settings", json={"analytics_cameras_max": 0},
                          headers=admin_headers).status_code == 400
        assert client.put("/api/settings", json={"analytics_cameras_max": 17},
                          headers=admin_headers).status_code == 400
        assert client.put("/api/settings", json={"analytics_cameras_max": 16},
                          headers=admin_headers).status_code == 200
    finally:
        client.put("/api/settings", json={"analytics_cameras_max": int(before)},
                   headers=admin_headers)
