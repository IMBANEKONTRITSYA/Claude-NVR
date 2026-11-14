"""PTZ-управление (SPEC §4: «PTZ-управление (если поддерживается камерой)»).

Тесты протокольного слоя (onvif_client) и HTTP-обёртки воркера
(onvif_api) — без реальной камеры, тем же способом, что и остальной ONVIF:
urlopen подменяется, SOAP-ответы берутся по образцу спецификации ONVIF
PTZ Service.

Главное, что здесь проверяется, — не «команда уходит», а два свойства, при
нарушении которых система остаётся внешне рабочей:

1. **ContinuousMove всегда несёт Timeout.** Без него камера, не получившая
   Stop (порвалась сеть, умерла вкладка), крутится до ручного вмешательства.
   Ошибка невидима на столе: локально Stop всегда доходит.
2. **PTZ-команды идут в профиль с PTZConfiguration.** Первый профиль списка
   — обычно субпоток, и он отвечает на PTZ-команды отказом. Проявилось бы
   как «пульт не работает» на конкретных прошивках.
"""
import pytest

import onvif_client as oc


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_urlopen(monkeypatch, response_bytes: bytes = b"<ok/>", capture: dict | None = None,
                  exc: Exception | None = None):
    def fake(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["body"] = req.data.decode("utf-8")
        if exc is not None:
            raise exc
        return _FakeResponse(response_bytes)

    monkeypatch.setattr(oc.urllib.request, "urlopen", fake)


PROFILES_WITH_PTZ = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:trt="http://www.onvif.org/ver10/media/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body><trt:GetProfilesResponse>
    <trt:Profiles token="sub_1"><tt:Name>SubStream</tt:Name>
      <tt:VideoEncoderConfiguration><tt:Resolution>
        <tt:Width>640</tt:Width><tt:Height>360</tt:Height></tt:Resolution>
      </tt:VideoEncoderConfiguration>
    </trt:Profiles>
    <trt:Profiles token="main_1"><tt:Name>MainStream</tt:Name>
      <tt:VideoEncoderConfiguration><tt:Resolution>
        <tt:Width>1920</tt:Width><tt:Height>1080</tt:Height></tt:Resolution>
      </tt:VideoEncoderConfiguration>
      <tt:PTZConfiguration token="ptz_cfg_1"><tt:Name>PTZ</tt:Name></tt:PTZConfiguration>
    </trt:Profiles>
  </trt:GetProfilesResponse></s:Body>
</s:Envelope>"""


# ---------------------------------------------------------------------------
# Выбор профиля
# ---------------------------------------------------------------------------

def test_get_profiles_reports_ptz_capability(monkeypatch):
    _mock_urlopen(monkeypatch, PROFILES_WITH_PTZ)
    profiles = oc.get_profiles("192.168.1.64", 80, "admin", "s3cret")
    assert [p["ptz"] for p in profiles] == [False, True]


def test_select_ptz_profile_skips_profile_without_ptz_configuration():
    """Ровно та ошибка, ради которой признак и разбирается: субпоток идёт в
    списке первым, а PTZ-команды принимает только основной профиль."""
    profiles = [
        {"token": "sub_1", "name": "Sub", "ptz": False},
        {"token": "main_1", "name": "Main", "ptz": True},
    ]
    assert oc.select_ptz_profile(profiles)["token"] == "main_1"


def test_select_ptz_profile_returns_none_for_fixed_camera():
    """Фиксированная камера — не ошибка: интерфейс просто не рисует пульт."""
    assert oc.select_ptz_profile([{"token": "main_1", "ptz": False}]) is None
    assert oc.select_ptz_profile([]) is None


# ---------------------------------------------------------------------------
# ContinuousMove
# ---------------------------------------------------------------------------

def test_continuous_move_always_carries_a_timeout(monkeypatch):
    """Страховка от бесконечного поворота: камера обязана остановиться сама,
    если следующая команда не пришла."""
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.continuous_move("192.168.1.64", 80, "main_1", pan=0.5, username="admin", password="s3cret")
    assert f"<Timeout>PT{oc.PTZ_MOVE_TIMEOUT_SEC}S</Timeout>" in cap["body"]
    assert oc.PTZ_MOVE_TIMEOUT_SEC > 0


def test_continuous_move_posts_velocity_in_schema_namespace(monkeypatch):
    """PanTilt/Zoom — в namespace схемы (tt), а не PTZ-сервиса: прошивки,
    сверяющие запрос с WSDL, иначе отвечают отказом."""
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.continuous_move("192.168.1.64", 8000, "main_1", pan=0.5, tilt=-0.25, zoom=0.1)
    assert cap["url"] == "http://192.168.1.64:8000/onvif/PTZ"
    assert f'<PanTilt xmlns="{oc._SCHEMA_NS}" x="0.500" y="-0.250"/>' in cap["body"]
    assert f'<Zoom xmlns="{oc._SCHEMA_NS}" x="0.100"/>' in cap["body"]
    assert "<ProfileToken>main_1</ProfileToken>" in cap["body"]


def test_continuous_move_clamps_velocity_out_of_range(monkeypatch):
    """Скорость вне [-1, 1] часть прошивок трактует по модулю — камера
    уезжает в сторону, противоположную нажатой стрелке."""
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.continuous_move("192.168.1.64", 80, "main_1", pan=7.5, tilt=-3.0, zoom=float("nan"))
    assert 'x="1.000" y="-1.000"' in cap["body"]
    assert '<Zoom xmlns="http://www.onvif.org/ver10/schema" x="0.000"/>' in cap["body"]


def test_clamp_velocity_handles_nan_and_garbage():
    assert oc.clamp_velocity(float("nan")) == 0.0
    assert oc.clamp_velocity(None) == 0.0
    assert oc.clamp_velocity("не число") == 0.0
    assert oc.clamp_velocity(0.42) == 0.42


def test_continuous_move_escapes_profile_token(monkeypatch):
    """Токен приходит от камеры, то есть из сети: подстановка в XML без
    экранирования позволяет вывалиться за пределы элемента."""
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.continuous_move("192.168.1.64", 80, "a</ProfileToken><Bogus>x")
    assert "<Bogus>" not in cap["body"]
    assert "&lt;Bogus&gt;" in cap["body"]


def test_continuous_move_reports_network_failure_as_onvif_error(monkeypatch):
    _mock_urlopen(monkeypatch, exc=OSError("сеть недоступна"))
    with pytest.raises(oc.OnvifError):
        oc.continuous_move("192.168.1.64", 80, "main_1")


# ---------------------------------------------------------------------------
# Stop и пресеты
# ---------------------------------------------------------------------------

def test_ptz_stop_halts_both_pan_tilt_and_zoom(monkeypatch):
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.ptz_stop("192.168.1.64", 80, "main_1", "admin", "s3cret")
    assert "<PanTilt>true</PanTilt>" in cap["body"]
    assert "<Zoom>true</Zoom>" in cap["body"]
    assert cap["url"].endswith("/onvif/PTZ")


PRESETS_RESPONSE = """<?xml version="1.0"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
            xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl"
            xmlns:tt="http://www.onvif.org/ver10/schema">
  <s:Body><tptz:GetPresetsResponse>
    <tptz:Preset token="1"><tt:Name>Ворота</tt:Name></tptz:Preset>
    <tptz:Preset token="2"><tt:Name>Парковка</tt:Name></tptz:Preset>
    <tptz:Preset><tt:Name>Без токена</tt:Name></tptz:Preset>
  </tptz:GetPresetsResponse></s:Body>
</s:Envelope>""".encode()


def test_get_presets_parses_token_and_name(monkeypatch):
    _mock_urlopen(monkeypatch, PRESETS_RESPONSE)
    presets = oc.get_presets("192.168.1.64", 80, "main_1", "admin", "s3cret")
    # Пресет без token отбрасывается: перейти на него всё равно нечем.
    assert presets == [
        {"token": "1", "name": "Ворота"},
        {"token": "2", "name": "Парковка"},
    ]


def test_get_presets_of_camera_without_presets_is_empty_not_error(monkeypatch):
    _mock_urlopen(monkeypatch, b"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'><s:Body/></s:Envelope>")
    assert oc.get_presets("192.168.1.64", 80, "main_1") == []


def test_goto_preset_sends_both_tokens(monkeypatch):
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.goto_preset("192.168.1.64", 80, "main_1", "3", "admin", "s3cret")
    assert "<ProfileToken>main_1</ProfileToken>" in cap["body"]
    assert "<PresetToken>3</PresetToken>" in cap["body"]


def test_set_preset_returns_token_assigned_by_camera(monkeypatch):
    cap: dict = {}
    _mock_urlopen(monkeypatch, b"""<?xml version="1.0"?>
        <s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
                    xmlns:tptz="http://www.onvif.org/ver20/ptz/wsdl">
          <s:Body><tptz:SetPresetResponse>
            <tptz:PresetToken>7</tptz:PresetToken>
          </tptz:SetPresetResponse></s:Body>
        </s:Envelope>""", capture=cap)
    assert oc.set_preset("192.168.1.64", 80, "main_1", "Касса") == "7"
    assert "<PresetName>Касса</PresetName>" in cap["body"]


def test_set_preset_without_token_in_response_is_not_a_failure(monkeypatch):
    """Позиция сохранена, токен прошивка не вернула — следующий GetPresets
    её покажет, и ронять операцию из-за этого нельзя."""
    _mock_urlopen(monkeypatch, b"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'><s:Body/></s:Envelope>")
    assert oc.set_preset("192.168.1.64", 80, "main_1", "Касса") is None


def test_set_preset_escapes_name(monkeypatch):
    """Имя вводит оператор — в XML оно попадает как есть."""
    cap: dict = {}
    _mock_urlopen(monkeypatch, b"<s:Envelope xmlns:s='http://www.w3.org/2003/05/soap-envelope'><s:Body/></s:Envelope>", capture=cap)
    oc.set_preset("192.168.1.64", 80, "main_1", "Вход <главный> & боковой")
    assert "&lt;главный&gt;" in cap["body"] and "&amp;" in cap["body"]


def test_ptz_requests_carry_credentials(monkeypatch):
    """PTZ-команды на камере закрыты аутентификацией — без заголовка
    WS-Security камера отвечает NotAuthorized, а пульт «молчит»."""
    cap: dict = {}
    _mock_urlopen(monkeypatch, capture=cap)
    oc.continuous_move("192.168.1.64", 80, "main_1", pan=0.5, username="admin", password="s3cret")
    assert "<UsernameToken>" in cap["body"]
    assert "<Username>admin</Username>" in cap["body"]
    # Пароль уходит только дайджестом, не открытым текстом.
    assert "s3cret" not in cap["body"]


# ---------------------------------------------------------------------------
# HTTP-обёртка воркера (onvif_api)
# ---------------------------------------------------------------------------

from fastapi import FastAPI                     # noqa: E402 — после тестов протокола
from fastapi.testclient import TestClient       # noqa: E402

import onvif_api                                # noqa: E402

CAM = {"host": "192.168.1.64", "port": 80, "username": "admin", "password": "s3cret"}


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(onvif_api.router)
    return TestClient(app)


def test_capabilities_reports_ptz_camera_with_presets(monkeypatch):
    monkeypatch.setattr(onvif_api, "get_profiles", lambda *a: [
        {"token": "sub_1", "name": "Sub", "ptz": False},
        {"token": "main_1", "name": "Main", "ptz": True},
    ])
    monkeypatch.setattr(onvif_api, "get_presets", lambda *a: [{"token": "1", "name": "Ворота"}])

    body = _client().post("/onvif/ptz/capabilities", json=CAM).json()

    assert body["ok"] is True and body["supported"] is True
    assert body["profile_token"] == "main_1"
    assert body["presets"] == [{"token": "1", "name": "Ворота"}]


def test_capabilities_of_fixed_camera_is_success_not_error(monkeypatch):
    """Неповоротная камера — не сбой: интерфейс просто не рисует пульт.
    Ответ ok:false заставил бы его показать ошибку на каждой обычной камере."""
    monkeypatch.setattr(onvif_api, "get_profiles", lambda *a: [{"token": "main_1", "ptz": False}])

    body = _client().post("/onvif/ptz/capabilities", json=CAM).json()

    assert body["ok"] is True
    assert body["supported"] is False
    assert body["presets"] == []


def test_capabilities_survives_camera_without_getpresets(monkeypatch):
    """GetPresets поддерживают не все прошивки — отсутствие пресетов не
    должно означать «камера не поворотная»."""
    monkeypatch.setattr(onvif_api, "get_profiles", lambda *a: [{"token": "main_1", "ptz": True}])

    def _no_presets(*a):
        raise oc.OnvifError("ActionNotSupported")

    monkeypatch.setattr(onvif_api, "get_presets", _no_presets)

    body = _client().post("/onvif/ptz/capabilities", json=CAM).json()

    assert body["supported"] is True and body["presets"] == []


def test_capabilities_reports_unreachable_camera_without_500(monkeypatch):
    def _fail(*a):
        raise oc.OnvifError("нет связи")

    monkeypatch.setattr(onvif_api, "get_profiles", _fail)

    r = _client().post("/onvif/ptz/capabilities", json=CAM)

    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "нет связи", "supported": False, "presets": []}


def test_move_uses_supplied_profile_token_without_extra_getprofiles(monkeypatch):
    """Токен от интерфейса избавляет от GetProfiles на каждое нажатие
    стрелки: при удержании кнопки это два лишних SOAP-запроса в секунду к
    камере, которая в этот момент занята поворотом."""
    calls: dict = {"profiles": 0, "move": None}

    def _profiles(*a):
        calls["profiles"] += 1
        return [{"token": "main_1", "ptz": True}]

    monkeypatch.setattr(onvif_api, "get_profiles", _profiles)
    monkeypatch.setattr(onvif_api, "continuous_move",
                        lambda *a, **kw: calls.__setitem__("move", a))

    body = _client().post("/onvif/ptz/move", json={**CAM, "profile_token": "main_1", "pan": 0.5}).json()

    assert body["ok"] is True and body["profile_token"] == "main_1"
    assert calls["profiles"] == 0
    assert calls["move"][2] == "main_1" and calls["move"][3] == 0.5


def test_move_resolves_ptz_profile_when_token_not_supplied(monkeypatch):
    monkeypatch.setattr(onvif_api, "get_profiles", lambda *a: [
        {"token": "sub_1", "ptz": False}, {"token": "main_1", "ptz": True},
    ])
    seen: dict = {}
    monkeypatch.setattr(onvif_api, "continuous_move", lambda *a, **kw: seen.update(token=a[2]))

    body = _client().post("/onvif/ptz/move", json={**CAM, "tilt": -0.3}).json()

    assert body["ok"] is True
    assert seen["token"] == "main_1"


def test_move_on_fixed_camera_reports_error(monkeypatch):
    monkeypatch.setattr(onvif_api, "get_profiles", lambda *a: [{"token": "main_1", "ptz": False}])

    body = _client().post("/onvif/ptz/move", json={**CAM, "pan": 0.5}).json()

    assert body["ok"] is False
    assert "PTZ" in body["error"]


@pytest.mark.parametrize("field,value", [
    ("pan", 5.0), ("pan", -5.0), ("tilt", 1.5), ("zoom", -2.0),
])
def test_move_rejects_velocity_outside_normalized_range(field, value):
    """Границы проверяются до похода к камере: прошивки трактуют выход за
    диапазон по-разному, вплоть до движения в обратную сторону."""
    r = _client().post("/onvif/ptz/move", json={**CAM, field: value})
    assert r.status_code == 422


def test_stop_reports_error_without_500(monkeypatch):
    monkeypatch.setattr(onvif_api, "get_profiles", lambda *a: [{"token": "main_1", "ptz": True}])

    def _fail(*a, **kw):
        raise oc.OnvifError("камера отвалилась")

    monkeypatch.setattr(onvif_api, "ptz_stop", _fail)

    r = _client().post("/onvif/ptz/stop", json={**CAM, "profile_token": "main_1"})

    assert r.status_code == 200
    assert r.json()["ok"] is False


def test_goto_preset_passes_preset_token(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(onvif_api, "goto_preset", lambda *a, **kw: seen.update(profile=a[2], preset=a[3]))

    body = _client().post("/onvif/ptz/preset/goto",
                          json={**CAM, "profile_token": "main_1", "preset_token": "7"}).json()

    assert body["ok"] is True
    assert seen == {"profile": "main_1", "preset": "7"}


def test_goto_preset_requires_preset_token():
    assert _client().post("/onvif/ptz/preset/goto", json={**CAM}).status_code == 422


def test_save_preset_returns_refreshed_list(monkeypatch):
    """Список возвращается сразу, чтобы интерфейсу не пришлось делать второй
    запрос ради только что созданной позиции."""
    monkeypatch.setattr(onvif_api, "set_preset", lambda *a, **kw: "3")
    monkeypatch.setattr(onvif_api, "get_presets", lambda *a: [
        {"token": "1", "name": "Ворота"}, {"token": "3", "name": "Касса"},
    ])

    body = _client().post("/onvif/ptz/preset/save",
                          json={**CAM, "profile_token": "main_1", "name": "Касса"}).json()

    assert body["ok"] is True and body["preset_token"] == "3"
    assert [p["name"] for p in body["presets"]] == ["Ворота", "Касса"]


def test_save_preset_rejects_empty_name():
    r = _client().post("/onvif/ptz/preset/save", json={**CAM, "name": ""})
    assert r.status_code == 422
