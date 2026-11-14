"""Пояс показа времени: определение из системы, проверка имени, доступ ролей.

Хранение остаётся UTC; настройка — только представление (см.
services/display_timezone.py). Здесь проверяется то, от чего зависит
правильность часов в интерфейсе: какое начальное значение получит объект и
кто его видит.
"""
import pytest

from app.services.display_timezone import detect_system_timezone, is_valid_timezone


@pytest.mark.parametrize("name", ["UTC", "Europe/Moscow", "Asia/Yekaterinburg", "America/New_York"])
def test_known_iana_names_are_valid(name):
    assert is_valid_timezone(name)


@pytest.mark.parametrize("name", ["", None, "Mars/Olympus", "UTC+5", " Europe/Moscow",
                                  "../../etc/passwd", "/etc/localtime", "Europe\\Moscow"])
def test_bad_names_are_rejected(name):
    assert not is_valid_timezone(name)


def test_explicit_env_wins(tmp_path):
    (tmp_path / "timezone").write_text("Europe/Moscow\n")
    env = {"FACEWATCH_TIMEZONE": "Asia/Yekaterinburg", "TZ": "Europe/Berlin"}
    assert detect_system_timezone(env, str(tmp_path / "timezone"), str(tmp_path / "none")) == "Asia/Yekaterinburg"


def test_tz_env_with_colon_prefix(tmp_path):
    assert detect_system_timezone({"TZ": ":Europe/Berlin"}, str(tmp_path / "x"), str(tmp_path / "y")) == "Europe/Berlin"


def test_posix_tz_string_falls_through_to_etc_timezone(tmp_path):
    """`TZ=UTC-5` — POSIX-строка, не имя IANA; угадывать по ней нельзя."""
    (tmp_path / "timezone").write_text("Asia/Omsk\n")
    assert detect_system_timezone({"TZ": "UTC-5"}, str(tmp_path / "timezone"), str(tmp_path / "y")) == "Asia/Omsk"


def test_localtime_symlink_is_used(tmp_path):
    zone = tmp_path / "usr/share/zoneinfo/Asia/Novosibirsk"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(b"TZif")
    link = tmp_path / "localtime"
    link.symlink_to(zone)
    assert detect_system_timezone({}, str(tmp_path / "none"), str(link)) == "Asia/Novosibirsk"


def test_nothing_known_falls_back_to_utc(tmp_path):
    assert detect_system_timezone({}, str(tmp_path / "x"), str(tmp_path / "y")) == "UTC"


# --- API -------------------------------------------------------------------

@pytest.fixture()
def restore_tz(client, admin_headers):
    before = client.get("/api/settings/client", headers=admin_headers).json()["display_timezone"]
    yield
    client.put("/api/settings", json={"display_timezone": before}, headers=admin_headers)


def test_seeded_value_is_a_valid_zone(client, admin_headers):
    """Сид при первом запуске записал действующий пояс, а не пустую строку."""
    tz = client.get("/api/settings/client", headers=admin_headers).json()["display_timezone"]
    assert is_valid_timezone(tz), tz


def test_admin_can_change_and_every_role_sees_it(client, admin_headers, make_user_headers, request, restore_tz):
    r = client.put("/api/settings", json={"display_timezone": "Asia/Yekaterinburg"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    # Время показывают все роли — Стена есть и у наблюдателя (§18).
    for role in ("operator", "viewer"):
        h = make_user_headers(f"tz_{role}_{request.node.name}"[:60], role)
        got = client.get("/api/settings/client", headers=h)
        assert got.status_code == 200, got.text
        assert got.json()["display_timezone"] == "Asia/Yekaterinburg"


def test_unknown_zone_is_refused_and_nothing_changes(client, admin_headers, restore_tz):
    before = client.get("/api/settings/client", headers=admin_headers).json()["display_timezone"]
    r = client.put("/api/settings", json={"display_timezone": "Mars/Olympus"}, headers=admin_headers)
    assert r.status_code == 400
    assert client.get("/api/settings/client", headers=admin_headers).json()["display_timezone"] == before


def test_only_admin_changes_the_zone(client, make_user_headers, request):
    h = make_user_headers(f"tz_op_{request.node.name}"[:60], "operator")
    r = client.put("/api/settings", json={"display_timezone": "Europe/Moscow"}, headers=h)
    assert r.status_code == 403
