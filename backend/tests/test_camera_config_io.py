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

import pytest

from app.services import camera_config


@pytest.fixture()
def cam_residue(client, admin_headers):
    """Убирает камеры, появившиеся за время теста.

    Фикстура `make_camera` из conftest.py тут не помогает: камеры заводит
    сам импорт, и их id тест заранее не знает. Снимок «что было до» и
    удаление всего нового покрывает и этот случай, и случай, когда импорт
    неожиданно применился вопреки ожиданию теста, — иначе упавший тест
    оставит камеры в БД и уронит следующий прогон (сторож
    test_zz_suite_leaves_no_residue.py и урок цикла 21).
    """
    def _ids():
        return {c["id"] for c in client.get("/api/cameras", headers=admin_headers).json()}

    before = _ids()
    yield
    for cam_id in _ids() - before:
        client.delete(f"/api/cameras/{cam_id}", headers=admin_headers)


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
