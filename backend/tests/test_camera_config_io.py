"""Импорт/экспорт конфигурации камер (SPEC §3) — на реальном Postgres+Redis.

Проверяется не формат ради формата, а три свойства, ради которых функция
существует:

1. Круговой сценарий: выгрузил → поправил поле → загрузил обратно, и
   камера изменилась ровно в этом поле, не потеряв пароль RTSP (который в
   выгрузке вырезан).
2. Файл по умолчанию не раздаёт учётные данные камер (§14).
3. Импорт применяется целиком или не применяется вовсе: строка с ошибкой
   посреди файла не должна оставить парк камер наполовину обновлённым.
"""
import io
import json

import pytest

from app.services import camera_config


# Фикстура `cam_residue` (уборка камер, id которых тест заранее не знает)
# с цикла 36 живёт в conftest.py: она понадобилась и тестам режима записи
# по движению (test_record_on_motion_api.py).


# --- разбор и маскирование (чистые функции, БД не нужна) --------------------

def test_mask_hides_password_but_keeps_login_and_path():
    masked = camera_config.mask_rtsp_url("rtsp://admin:s3cret@10.0.0.5:554/Streaming/Channels/101")
    assert "s3cret" not in masked
    assert masked == "rtsp://admin:***@10.0.0.5:554/Streaming/Channels/101"
    assert camera_config.is_masked(masked)


def test_mask_leaves_url_without_credentials_untouched():
    url = "rtsp://10.0.0.5:554/stream1"
    assert camera_config.mask_rtsp_url(url) == url
    assert not camera_config.is_masked(url)


def test_csv_roundtrip_preserves_values():
    rows = [{
        "name": "Проходная", "location": "1 этаж", "enabled": True, "mode": "analytics",
        "rtsp_url": "rtsp://a:b@10.0.0.1/s", "sub_rtsp_url": "", "motion_sensitivity": 40,
        "retention_days": None, "onvif_enabled": False, "onvif_host": "", "onvif_port": None,
        "onvif_username": "",
    }]
    parsed = camera_config.parse_file(camera_config.rows_to_csv(rows).encode("utf-8"))
    assert len(parsed) == 1
    assert parsed[0]["name"] == "Проходная"
    assert parsed[0]["mode"] == "analytics"
    assert parsed[0]["enabled"] == "true"
    assert parsed[0]["retention_days"] == ""


def test_json_export_is_reimportable():
    rows = [{"name": "Cam", "rtsp_url": "rtsp://10.0.0.2/s", "enabled": True}]
    parsed = camera_config.parse_file(camera_config.rows_to_json(rows).encode("utf-8"))
    assert parsed[0]["name"] == "Cam"


def test_json_accepts_bare_list():
    parsed = camera_config.parse_file(b'[{"name": "Cam", "rtsp_url": "rtsp://10.0.0.2/s"}]')
    assert parsed[0]["name"] == "Cam"


def test_csv_without_name_column_is_rejected_with_hint():
    try:
        camera_config.parse_file(b"location,mode\nfoo,record_only\n")
        raise AssertionError("файл без обязательной колонки принят")
    except camera_config.ImportError_ as e:
        assert "name" in str(e)


def test_blank_trailing_lines_are_ignored():
    parsed = camera_config.parse_file(b"name,rtsp_url\nCam,rtsp://10.0.0.2/s\n,\n\n")
    assert len(parsed) == 1


# --- сквозной путь через API -----------------------------------------------

def _make_camera(client, admin_headers, name, url="rtsp://admin:s3cret@10.9.9.9:554/main", **extra):
    payload = {"name": name, "rtsp_url": url, "location": "склад", **extra}
    r = client.post("/api/cameras", json=payload, headers=admin_headers)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _export(client, admin_token, fmt="csv", secrets=False):
    r = client.get(f"/api/cameras/export?format={fmt}&include_secrets={'1' if secrets else '0'}&token={admin_token}")
    assert r.status_code == 200, r.text
    return r.text


def _import(client, admin_headers, body: str, filename="cameras.csv", dry_run=False):
    return client.post(
        f"/api/cameras/import?dry_run={'1' if dry_run else '0'}",
        files={"file": (filename, io.BytesIO(body.encode("utf-8")), "text/csv")},
        headers=admin_headers,
    )


def test_export_hides_rtsp_password_by_default(client, admin_headers, admin_token, request, cam_residue):
    cam_id = _make_camera(client, admin_headers, f"exp_{request.node.name}"[:60])
    try:
        body = _export(client, admin_token)
        assert "s3cret" not in body, "выгрузка по умолчанию раздала пароль камеры"
        assert "***" in body
    finally:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_export_with_secrets_returns_full_url(client, admin_headers, admin_token, request, cam_residue):
    cam_id = _make_camera(client, admin_headers, f"exps_{request.node.name}"[:60])
    try:
        assert "s3cret" in _export(client, admin_token, secrets=True)
    finally:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_roundtrip_edit_keeps_password_and_applies_change(client, admin_headers, admin_token, request, cam_residue):
    """Главный сценарий §3: выгрузка без секретов правится и грузится назад."""
    name = f"rt_{request.node.name}"[:60]
    cam_id = _make_camera(client, admin_headers, name)
    try:
        body = _export(client, admin_token)
        edited = body.replace("склад", "проходная")
        r = _import(client, admin_headers, edited)
        assert r.status_code == 200, r.text
        assert r.json()["ok"] and r.json()["updated"] >= 1 and r.json()["created"] == 0

        cams = {c["name"]: c for c in client.get("/api/cameras", headers=admin_headers).json()}
        assert cams[name]["location"] == "проходная"
        # Пароль не потерян: маскированный URL означал «оставить как было»
        rtsp = client.get(f"/api/cameras/{cam_id}/rtsp", headers=admin_headers).json()
        assert "s3cret" in rtsp["rtsp_url"]
        # Дублей не создано
        assert sum(1 for c in cams.values() if c["name"] == name) == 1
    finally:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_import_creates_new_camera(client, admin_headers, request, cam_residue):
    name = f"new_{request.node.name}"[:60]
    body = f"name,rtsp_url,location,mode\n{name},rtsp://u:p@10.1.2.3:554/s,вход,record_only\n"
    r = _import(client, admin_headers, body)
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "dry_run": False, "total": 1, "created": 1, "updated": 0, "errors": []}
    cams = {c["name"]: c for c in client.get("/api/cameras", headers=admin_headers).json()}
    try:
        assert cams[name]["location"] == "вход"
    finally:
        client.delete(f"/api/cameras/{cams[name]['id']}", headers=admin_headers)


def test_masked_url_for_unknown_camera_is_an_error(client, admin_headers, request, cam_residue):
    name = f"mask_{request.node.name}"[:60]
    body = f"name,rtsp_url\n{name},rtsp://admin:***@10.1.2.3:554/s\n"
    r = _import(client, admin_headers, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert not data["ok"] and data["errors"][0]["row"] == 1
    assert "***" in data["errors"][0]["error"]
    assert all(c["name"] != name for c in client.get("/api/cameras", headers=admin_headers).json())


def test_bad_row_rolls_back_whole_file(client, admin_headers, request, cam_residue):
    """Всё или ничего: корректная первая строка не должна примениться,
    если во второй ошибка."""
    ok_name = f"ok_{request.node.name}"[:60]
    body = (
        "name,rtsp_url\n"
        f"{ok_name},rtsp://u:p@10.1.2.3:554/s\n"
        f"bad_{ok_name},http://10.1.2.4/not-rtsp\n"
    )
    r = _import(client, admin_headers, body)
    assert r.status_code == 200, r.text
    data = r.json()
    assert not data["ok"]
    assert data["created"] == 0 and data["updated"] == 0
    assert [e["row"] for e in data["errors"]] == [2]
    names = {c["name"] for c in client.get("/api/cameras", headers=admin_headers).json()}
    assert ok_name not in names, "строка до ошибочной всё же применилась"


def test_duplicate_names_in_file_are_rejected(client, admin_headers, request, cam_residue):
    name = f"dup_{request.node.name}"[:60]
    body = f"name,rtsp_url\n{name},rtsp://u:p@10.1.2.3/s\n{name},rtsp://u:p@10.1.2.4/s\n"
    data = _import(client, admin_headers, body).json()
    assert not data["ok"] and "дважды" in data["errors"][0]["error"]
    assert all(c["name"] != name for c in client.get("/api/cameras", headers=admin_headers).json())


def test_dry_run_reports_but_changes_nothing(client, admin_headers, request, cam_residue):
    name = f"dry_{request.node.name}"[:60]
    body = f"name,rtsp_url\n{name},rtsp://u:p@10.1.2.3:554/s\n"
    data = _import(client, admin_headers, body, dry_run=True).json()
    assert data["ok"] and data["dry_run"] and data["created"] == 1
    assert all(c["name"] != name for c in client.get("/api/cameras", headers=admin_headers).json())


def test_import_respects_analytics_limit(client, admin_headers, request, cam_residue):
    """§1/§24: предел камер аналитики считается по итогу файла, а не построчно."""
    limit = client.get("/api/settings", headers=admin_headers).json().get("analytics_cameras_max")
    limit = int(limit) if limit else 2
    names = [f"an{i}_{request.node.name}"[:60] for i in range(limit + 1)]
    body = "name,rtsp_url,mode\n" + "".join(
        f"{n},rtsp://u:p@10.5.5.{i + 1}:554/s,analytics\n" for i, n in enumerate(names)
    )
    data = _import(client, admin_headers, body).json()
    assert not data["ok"], "импорт завёл камер аналитики сверх предела"
    assert "предел" in data["errors"][-1]["error"]
    existing = {c["name"] for c in client.get("/api/cameras", headers=admin_headers).json()}
    assert not existing & set(names)


def test_operator_cannot_export_or_import(client, make_user, request):
    _, token = make_user(f"op_{request.node.name}"[:60], "operator")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get(f"/api/cameras/export?token={token}").status_code == 403
    r = client.post(
        "/api/cameras/import",
        files={"file": ("c.csv", io.BytesIO(b"name,rtsp_url\nX,rtsp://u:p@1.2.3.4/s\n"), "text/csv")},
        headers=headers,
    )
    assert r.status_code == 403


def test_import_rejects_garbage_file(client, admin_headers, cam_residue):
    r = _import(client, admin_headers, "не файл конфигурации вовсе", filename="x.txt")
    assert r.status_code == 400


# --- расписание детекции и зоны детекции в файле (§3 + §6 + §11) -----------
#
# До этого файл конфигурации нёс только «плоские» поля камеры. Расписание
# детекции (§6) и полигоны ROI (§11 «Настройки по камерам: retention, ROI,
# режим») в нём отсутствовали, поэтому восстановление парка из выгрузки
# молча возвращало все камеры к «детекция круглосуточно по всему кадру».
# Тесты ниже стерегут ровно три свойства: значения переживают круг,
# старый файл без колонок ничего не стирает, пустая ячейка снимает
# настройку осознанно.

SCHEDULE = {"enabled": True, "windows": [{"days": [0, 1, 2, 3, 4], "start": "22:00", "end": "06:00"}]}
ROI = {"polygons": [[[0.1, 0.1], [0.9, 0.1], [0.9, 0.9]]]}


def test_csv_cell_carries_nested_structures_verbatim():
    rows = [{"name": "Cam", "rtsp_url": "rtsp://10.0.0.2/s",
             "detection_schedule": SCHEDULE, "roi": ROI}]
    csv_text = camera_config.rows_to_csv(rows)
    parsed = camera_config.parse_file(csv_text.encode("utf-8"))
    assert camera_config.parse_json_object(parsed[0]["detection_schedule"], "s") == SCHEDULE
    assert camera_config.parse_json_object(parsed[0]["roi"], "r") == ROI


def test_json_export_keeps_structures_as_objects_not_strings():
    """В JSON-выгрузке расписание — объект: файл читают и правят руками."""
    rows = [{"name": "Cam", "rtsp_url": "rtsp://10.0.0.2/s", "detection_schedule": SCHEDULE}]
    parsed = camera_config.parse_file(camera_config.rows_to_json(rows).encode("utf-8"))
    assert parsed[0]["detection_schedule"] == SCHEDULE
    assert camera_config.parse_json_object(parsed[0]["detection_schedule"], "s") == SCHEDULE


def test_empty_cell_is_absence_not_error():
    assert camera_config.parse_json_object("", "s") is None
    assert camera_config.parse_json_object("   ", "s") is None
    assert camera_config.parse_json_object(None, "s") is None


def test_broken_json_in_cell_names_the_field():
    for bad in ("{не json", "[1,2]", "42"):
        try:
            camera_config.parse_json_object(bad, "detection_schedule")
            raise AssertionError(f"принято значение {bad!r}")
        except ValueError as e:
            assert "detection_schedule" in str(e)


def test_camera_without_schedule_exports_empty_cell():
    """Пустая ячейка, а не «{}»: отсутствие расписания — это отсутствие."""
    class _Cam:
        name, location, enabled, mode = "Cam", "", True, "record_only"
        motion_sensitivity = retention_days = onvif_port = None
        onvif_enabled = False
        onvif_host = onvif_username = None
        detection_schedule = roi = None
        record_on_motion = False   # SPEC §6, добавлено циклом 36

    row = camera_config.camera_row(_Cam(), "rtsp://10.0.0.2/s", None, include_secrets=True)
    assert row["detection_schedule"] is None and row["roi"] is None
    assert ",," in camera_config.rows_to_csv([row])


def _schedule_of(client, headers, name):
    cams = {c["name"]: c for c in client.get("/api/cameras", headers=headers).json()}
    return cams[name]["detection_schedule"]


def test_roundtrip_preserves_schedule_and_roi(client, admin_headers, admin_token, request, cam_residue):
    """Круговой сценарий §3: правка локации не должна стоить расписания и зон."""
    name = f"sch_{request.node.name}"[:60]
    cam_id = _make_camera(client, admin_headers, name, mode="analytics", detection_schedule=SCHEDULE)
    try:
        assert client.put(f"/api/cameras/{cam_id}/roi", json=ROI, headers=admin_headers).status_code == 200

        body = _export(client, admin_token)
        r = _import(client, admin_headers, body.replace("склад", "проходная"))
        assert r.status_code == 200 and r.json()["ok"], r.text

        assert _schedule_of(client, admin_headers, name) == SCHEDULE
        assert client.get(f"/api/cameras/{cam_id}/roi", headers=admin_headers).json() == ROI
    finally:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_file_without_new_columns_keeps_schedule_and_roi(client, admin_headers, admin_token, request, cam_residue):
    """Файл прежней версии (без колонок) — «не трогать», а не «стереть».

    Иначе загрузка старого файла ради правки одной локации разом снимает
    ночные расписания и зоны со всего парка, ничего не сообщая.
    """
    name = f"old_{request.node.name}"[:60]
    cam_id = _make_camera(client, admin_headers, name, mode="analytics", detection_schedule=SCHEDULE)
    try:
        client.put(f"/api/cameras/{cam_id}/roi", json=ROI, headers=admin_headers)
        # Файл, каким его выгружала прежняя версия: только плоские колонки.
        old_body = f"name,rtsp_url,location\n{name},rtsp://admin:***@10.9.9.9:554/main,проходная\n"
        r = _import(client, admin_headers, old_body)
        assert r.status_code == 200 and r.json()["ok"], r.text

        assert _schedule_of(client, admin_headers, name) == SCHEDULE, "старый файл стёр расписание"
        assert client.get(f"/api/cameras/{cam_id}/roi", headers=admin_headers).json() == ROI
    finally:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_empty_cells_clear_schedule_and_roi(client, admin_headers, request, cam_residue):
    """Пустая ячейка при наличии колонки — осознанное снятие настройки."""
    name = f"clr_{request.node.name}"[:60]
    cam_id = _make_camera(client, admin_headers, name, mode="analytics", detection_schedule=SCHEDULE)
    try:
        client.put(f"/api/cameras/{cam_id}/roi", json=ROI, headers=admin_headers)
        body = (f"name,rtsp_url,detection_schedule,roi\n"
                f"{name},rtsp://admin:***@10.9.9.9:554/main,,\n")
        r = _import(client, admin_headers, body)
        assert r.status_code == 200 and r.json()["ok"], r.text

        assert _schedule_of(client, admin_headers, name) is None
        assert client.get(f"/api/cameras/{cam_id}/roi", headers=admin_headers).json() == {"polygons": []}
    finally:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


def test_import_creates_camera_with_schedule_and_roi_from_json_file(client, admin_headers, request, cam_residue):
    name = f"mk_{request.node.name}"[:60]
    body = json.dumps({"cameras": [{
        "name": name, "rtsp_url": "rtsp://u:p@10.1.2.3:554/s", "mode": "analytics",
        "detection_schedule": SCHEDULE, "roi": ROI,
    }]}, ensure_ascii=False)
    r = _import(client, admin_headers, body, filename="cameras.json")
    assert r.status_code == 200 and r.json()["created"] == 1, r.text
    cams = {c["name"]: c for c in client.get("/api/cameras", headers=admin_headers).json()}
    try:
        assert cams[name]["detection_schedule"] == SCHEDULE
        assert client.get(f"/api/cameras/{cams[name]['id']}/roi", headers=admin_headers).json() == ROI
    finally:
        client.delete(f"/api/cameras/{cams[name]['id']}", headers=admin_headers)


def test_invalid_schedule_in_file_rolls_back_whole_import(client, admin_headers, request, cam_residue):
    """Расписание проверяется той же моделью, что и форма (§6): «25:00» — не время."""
    name = f"bad_{request.node.name}"[:60]
    bad = json.dumps({"enabled": True, "windows": [{"days": [0], "start": "25:00", "end": "06:00"}]})
    body = ("name,rtsp_url,detection_schedule\n"
            f"{name},rtsp://u:p@10.1.2.3:554/s,\"{bad}\"\n")
    data = _import(client, admin_headers, body).json()
    assert not data["ok"] and data["created"] == 0
    assert data["errors"][0]["row"] == 1
    assert all(c["name"] != name for c in client.get("/api/cameras", headers=admin_headers).json())


def test_invalid_roi_in_file_is_reported(client, admin_headers, request, cam_residue):
    name = f"broi_{request.node.name}"[:60]
    body = ("name,rtsp_url,roi\n"
            f"{name},rtsp://u:p@10.1.2.3:554/s,\"{{\"\"polygons\"\": \"\"не список\"\"}}\"\n")
    data = _import(client, admin_headers, body).json()
    assert not data["ok"] and data["created"] == 0
    assert "polygons" in data["errors"][0]["error"]


def test_export_carries_settings_to_a_fresh_installation(client, admin_headers, admin_token, request, cam_residue):
    """§3 «перенос конфигурации с тестового стенда на боевой сервер».

    Отдельный тест, а не вариация кругового: при импорте поверх той же
    камеры расписание и зоны уцелели бы и от правила «колонки нет —
    не трогать», то есть круговой сценарий зелен даже с выгрузкой без
    этих колонок. На чистой установке подставить их неоткуда — здесь
    камера заводится файлом заново, и потеря видна.
    """
    name = f"mv_{request.node.name}"[:60]
    cam_id = _make_camera(client, admin_headers, name, mode="analytics", detection_schedule=SCHEDULE)
    client.put(f"/api/cameras/{cam_id}/roi", json=ROI, headers=admin_headers)
    body = _export(client, admin_token, secrets=True)
    client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)

    r = _import(client, admin_headers, body)
    assert r.status_code == 200 and r.json()["ok"], r.text
    cams = {c["name"]: c for c in client.get("/api/cameras", headers=admin_headers).json()}
    assert name in cams, "камера не заведена импортом"
    try:
        assert cams[name]["detection_schedule"] == SCHEDULE, "расписание не пережило перенос"
        assert client.get(f"/api/cameras/{cams[name]['id']}/roi", headers=admin_headers).json() == ROI
    finally:
        client.delete(f"/api/cameras/{cams[name]['id']}", headers=admin_headers)
