"""Имя камеры для массового добавления: OSD, скоупы, модель, IP.

Запрос был «можно ли подхватить название из OSD самой камеры». Можно:
ONVIF отдаёт текстовые наложения через GetOSDs Media-сервиса, и на реальной
камере пользователя (ST-VA5641) там лежат ровно две строки —
`<Дата и время>` и `NewEntraceKPP`. Вторая и есть название точки, которое
нужно подставить в имя камеры; первую надо отбросить.
"""
import pytest

pytest.importorskip("defusedxml", reason="ONVIF-клиент требует defusedxml")

from onvif_client import (  # noqa: E402
    _osd_name_candidate,
    parse_scope_name,
    suggest_camera_name,
)

# Ровно то, что показывает веб-интерфейс камеры пользователя.
REAL_CAMERA_OSD = ["<Дата и время>", "NewEntraceKPP"]


def test_picks_location_name_from_real_camera_osd():
    assert _osd_name_candidate(REAL_CAMERA_OSD) == "NewEntraceKPP"


@pytest.mark.parametrize("noise", [
    "<Дата и время>",
    "Дата и время",
    "Date and time",
    "2026/08/05 14:22:01",
    "05.08.2026",
    "14:22:01",
])
def test_date_and_time_osd_is_not_a_name(noise):
    """Дата и время есть почти на каждой камере и именем быть не могут —
    ни как подпись типа, ни как уже подставленное значение."""
    assert _osd_name_candidate([noise]) is None


def test_no_osd_at_all():
    assert _osd_name_candidate([]) is None


def test_osd_wins_over_scope_and_model():
    """OSD — самое человеческое из доступного: это то, чем администратор
    подписал камеру на самой картинке."""
    assert suggest_camera_name(
        {"scopes": ["onvif://www.onvif.org/name/ST-VA5641"], "host": "192.168.105.19"},
        {"Manufacturer": "ST", "Model": "ST-VA5641"},
        REAL_CAMERA_OSD,
    ) == "NewEntraceKPP"


def test_falls_back_to_scope_when_osd_is_only_datetime():
    assert suggest_camera_name(
        {"scopes": ["onvif://www.onvif.org/name/ST-VA5641"], "host": "192.168.105.19"},
        {"Model": "ST-VA5641"},
        ["<Дата и время>"],
    ) == "ST-VA5641"


def test_falls_back_to_manufacturer_and_model():
    """Камера, найденная перебором подсети, приходит без скоупов: проба
    GetSystemDateAndTime их не возвращает."""
    assert suggest_camera_name(
        {"scopes": [], "host": "192.168.105.19"},
        {"Manufacturer": "Hikvision", "Model": "DS-2CD2043"},
        [],
    ) == "Hikvision DS-2CD2043"


def test_falls_back_to_ip_when_nothing_known():
    assert suggest_camera_name({"scopes": [], "host": "192.168.105.19"}, {}, []) == "192.168.105.19"


def test_never_returns_empty():
    """Имя — обязательное поле камеры, пустым оно остаться не должно."""
    assert suggest_camera_name({}, None, None) == "Камера"


def test_scope_name_is_percent_decoded():
    """Имена с пробелами и кириллицей приезжают в скоупе кодированными."""
    assert parse_scope_name(["onvif://www.onvif.org/name/Hall%20Camera"]) == "Hall Camera"
    assert parse_scope_name(["onvif://www.onvif.org/name/%D0%9F%D1%80%D0%BE%D1%85%D0%BE%D0%B4%D0%BD%D0%B0%D1%8F"]) == "Проходная"


def test_scope_without_name_is_ignored():
    assert parse_scope_name(["onvif://www.onvif.org/location/city/Moscow"]) is None
