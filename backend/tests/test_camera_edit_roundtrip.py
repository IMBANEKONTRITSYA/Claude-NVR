"""Редактирование камеры не должно терять то, что не показано в форме.

Класс дефектов, о котором сообщил пользователь: `CameraOut` не отдавал
часть полей камеры, форма редактирования открывала их пустыми, а PUT
трактует пустое значение как «убрать». В результате обычная правка имени
камеры молча уносила её RTSP-адрес и ONVIF-настройки.

Проверяется именно **сквозной путь формы**: прочитать камеру тем же
запросом, каким её читает интерфейс, отправить обратно — и убедиться, что
ничего не пропало.
"""


def _put(client, headers, cam, **overrides):
    """PUT ровно теми полями, которые форма получает из `CameraOut`."""
    body = {
        "name": cam["name"],
        "rtsp_url": overrides.pop("rtsp_url", None) or "",
        "location": cam["location"],
        "enabled": cam["enabled"],
        "mode": cam["mode"],
        "retention_days": cam["retention_days"],
        "onvif_enabled": cam["onvif_enabled"],
        "onvif_host": cam.get("onvif_host") or "",
        "onvif_port": cam.get("onvif_port") or 80,
        "onvif_username": cam.get("onvif_username") or "",
        "onvif_password": "",
        **overrides,
    }
    return client.put(f"/api/cameras/{cam['id']}", json=body, headers=headers)


def test_rtsp_url_is_readable_for_the_edit_form(client, admin_headers, make_camera):
    """Форма обязана уметь получить адрес камеры: в списке его нет.

    Без этого «Изм.» открывала пустое поле адреса, и сохранение падало
    валидацией (адрес обязателен) — то есть отредактировать камеру было
    нельзя вообще, не набрав адрес заново руками.
    """
    cam = make_camera("edit-rtsp")
    r = client.get(f"/api/cameras/{cam['id']}/rtsp", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["rtsp_url"] == f"rtsp://cam/{cam['name']}"


def test_editing_name_keeps_rtsp_url(client, admin_headers, make_camera):
    """Правка имени с адресом из формы не меняет адрес."""
    cam = make_camera("edit-keep-rtsp")
    url = client.get(f"/api/cameras/{cam['id']}/rtsp", headers=admin_headers).json()["rtsp_url"]

    assert _put(client, admin_headers, cam, name="Новое имя", rtsp_url=url).status_code == 200

    after = client.get(f"/api/cameras/{cam['id']}/rtsp", headers=admin_headers).json()
    assert after["rtsp_url"] == url


def test_camera_list_exposes_onvif_settings_for_the_form(client, admin_headers, make_camera):
    """ONVIF-адрес, порт и логин должны приходить в списке.

    Пароль — намеренно нет: он write-only, пустое значение при PUT
    означает «оставить прежний».
    """
    cam = make_camera("edit-onvif-fields", onvif_enabled=True,
                      onvif_host="192.168.105.19", onvif_port=8080,
                      onvif_username="admin", onvif_password="secret")
    got = next(c for c in client.get("/api/cameras", headers=admin_headers).json()
               if c["id"] == cam["id"])

    assert got["onvif_host"] == "192.168.105.19"
    assert got["onvif_port"] == 8080
    assert got["onvif_username"] == "admin"
    assert "onvif_password" not in got


def test_editing_name_keeps_onvif_settings(client, admin_headers, make_camera):
    """Смысловая проверка: правка имени не должна отвязывать камеру от ONVIF.

    До фикса форма отправляла пустой `onvif_host` (его неоткуда было
    взять), PUT трактовал пустое значение как «убрать», и камера молча
    теряла ONVIF-привязку вместе с логином.
    """
    cam = make_camera("edit-keep-onvif", onvif_enabled=True,
                      onvif_host="192.168.105.19", onvif_port=8080,
                      onvif_username="admin", onvif_password="secret")
    url = client.get(f"/api/cameras/{cam['id']}/rtsp", headers=admin_headers).json()["rtsp_url"]
    fresh = next(c for c in client.get("/api/cameras", headers=admin_headers).json()
                 if c["id"] == cam["id"])

    assert _put(client, admin_headers, fresh, name="Переименована",
                rtsp_url=url).status_code == 200

    after = next(c for c in client.get("/api/cameras", headers=admin_headers).json()
                 if c["id"] == cam["id"])
    assert after["onvif_host"] == "192.168.105.19"
    assert after["onvif_port"] == 8080
    assert after["onvif_username"] == "admin"
    assert after["has_onvif"] is True


def test_onvif_host_can_still_be_cleared(client, admin_headers, make_camera):
    """Позитивный контроль: очистить привязку по-прежнему можно.

    Фикс не должен превратить «пустое поле» в «поле нельзя очистить» —
    иначе отвязать камеру от ONVIF стало бы нечем.
    """
    cam = make_camera("edit-clear-onvif", onvif_enabled=True,
                      onvif_host="192.168.105.19", onvif_username="admin")
    url = client.get(f"/api/cameras/{cam['id']}/rtsp", headers=admin_headers).json()["rtsp_url"]

    assert _put(client, admin_headers, cam, rtsp_url=url,
                onvif_enabled=False, onvif_host="", onvif_username="").status_code == 200

    after = next(c for c in client.get("/api/cameras", headers=admin_headers).json()
                 if c["id"] == cam["id"])
    assert after["onvif_host"] is None
    assert after["has_onvif"] is False


def test_editing_deleted_camera_reports_not_found(client, admin_headers, make_camera):
    """PUT на удалённую камеру отвечает 404 — на этот ответ опирается
    интерфейс, чтобы выйти из режима редактирования, а не упираться в
    «Камера не найдена» на каждом сохранении."""
    cam = make_camera("edit-vanished")
    client.delete(f"/api/cameras/{cam['id']}", headers=admin_headers)

    r = _put(client, admin_headers, cam, rtsp_url="rtsp://cam/x")
    assert r.status_code == 404
    assert "не найдена" in r.json()["detail"].lower()
