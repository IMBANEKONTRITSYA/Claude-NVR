"""Подстановка учётных данных в RTSP-URI и перебор подсети вместо multicast.

Обе функции закрывают то, на что наткнулся пользователь при добавлении
первой камеры:

1. «Получить профили потоков» отрабатывал, но подставленный в форму RTSP-URL
   давал `401 Unauthorized` на кнопке «Проверить RTSP». Камера по
   спецификации ONVIF возвращает адрес без учётных данных
   (`rtsp://192.168.105.19/media/video1`), а ffmpeg/OpenCV читают логин и
   пароль только из самого URL.
2. «Найти камеры в сети» не находил ничего. WS-Discovery — multicast, а
   воркер живёт в docker-контейнере на NAT'ированной сети; на Docker Desktop
   под Windows multicast до физической ЛВС не доходит вовсе.

Сеть здесь не трогается: `_is_onvif_device` подменяется, проверяются разбор
диапазона, ограничения и оркестрация.
"""
import pytest

pytest.importorskip("defusedxml", reason="ONVIF-клиент требует defusedxml")

from onvif_client import (  # noqa: E402
    MAX_SCAN_HOSTS,
    OnvifError,
    inject_credentials,
    scan_subnet,
)


# --- Подстановка учётных данных ---------------------------------------------

def test_credentials_injected_into_bare_uri():
    """Ровно случай пользователя: камера вернула адрес без учётных данных."""
    out = inject_credentials("rtsp://192.168.105.19/media/video1", "admin", "secret")
    assert out == "rtsp://admin:secret@192.168.105.19/media/video1"


def test_port_and_query_preserved():
    out = inject_credentials("rtsp://10.0.0.5:554/stream?ch=1", "user", "pw")
    assert out == "rtsp://user:pw@10.0.0.5:554/stream?ch=1"


def test_special_characters_are_percent_encoded():
    """Пароль вида `p@ss:w/ord` без кодирования развалил бы разбор URL:
    ffmpeg принял бы часть пароля за хост."""
    out = inject_credentials("rtsp://10.0.0.5/s", "adm:in", "p@ss:w/ord")
    assert out == "rtsp://adm%3Ain:p%40ss%3Aw%2Ford@10.0.0.5/s"
    # Хост должен остаться единственным, что стоит после последней '@'.
    assert out.rsplit("@", 1)[1] == "10.0.0.5/s"


def test_existing_credentials_are_not_overwritten():
    """Если адрес уже с учётными данными — администратор или камера знают
    лучше, перетирать нельзя."""
    uri = "rtsp://other:pw@10.0.0.5/s"
    assert inject_credentials(uri, "admin", "secret") == uri


def test_username_without_password():
    assert inject_credentials("rtsp://10.0.0.5/s", "admin", None) == "rtsp://admin@10.0.0.5/s"


@pytest.mark.parametrize("username", [None, ""])
def test_no_username_leaves_uri_untouched(username):
    uri = "rtsp://10.0.0.5/s"
    assert inject_credentials(uri, username, "pw") == uri


def test_empty_uri_is_passed_through():
    assert inject_credentials("", "admin", "pw") == ""


# --- Перебор подсети ---------------------------------------------------------

def _fake_probe(found_at):
    """Подменяет сетевую пробу: «камера» отвечает только на перечисленные
    адреса, остальное молчит."""
    def probe(host, port, timeout):
        if (host, port) in found_at:
            return {"address": "", "xaddrs": [f"http://{host}:{port}/onvif/device_service"],
                    "scopes": [], "host": host, "port": port}
        return None
    return probe


def test_scan_finds_device(monkeypatch):
    import onvif_client
    monkeypatch.setattr(onvif_client, "_is_onvif_device", _fake_probe({("192.168.105.19", 80)}))
    devices = scan_subnet("192.168.105.0/24", ports=(80,))
    assert [d["host"] for d in devices] == ["192.168.105.19"]


def test_device_answering_on_several_ports_is_reported_once(monkeypatch):
    """Камера может слушать и 80, и 8000 — в списке она должна быть одна,
    иначе администратор увидит дубли."""
    import onvif_client
    monkeypatch.setattr(
        onvif_client, "_is_onvif_device",
        _fake_probe({("192.168.1.7", 80), ("192.168.1.7", 8000)}),
    )
    devices = scan_subnet("192.168.1.0/24", ports=(80, 8000))
    assert len(devices) == 1
    assert devices[0]["port"] == 80, "оставляем первый порт по возрастанию"


def test_host_cidr_scans_single_address(monkeypatch):
    """/32 — способ проверить один конкретный адрес, он не должен давать
    пустой список хостов."""
    import onvif_client
    monkeypatch.setattr(onvif_client, "_is_onvif_device", _fake_probe({("192.168.105.19", 80)}))
    assert len(scan_subnet("192.168.105.19/32", ports=(80,))) == 1


def test_empty_result_when_nothing_answers(monkeypatch):
    import onvif_client
    monkeypatch.setattr(onvif_client, "_is_onvif_device", _fake_probe(set()))
    assert scan_subnet("192.168.1.0/24", ports=(80,)) == []


@pytest.mark.parametrize("bad", ["не-cidr", "192.168.1.0/33", "", "192.168.1.999/24"])
def test_invalid_cidr_rejected(bad):
    with pytest.raises(OnvifError):
        scan_subnet(bad)


def test_public_range_rejected():
    """NVR для локальной сети не должен уметь сканировать интернет —
    иначе это готовый инструмент разведки в чужих руках."""
    with pytest.raises(OnvifError, match="приватная"):
        scan_subnet("8.8.8.0/24")


@pytest.mark.parametrize("private", ["10.0.0.0/24", "172.16.5.0/24", "192.168.1.0/24", "169.254.1.0/24"])
def test_private_ranges_allowed(private, monkeypatch):
    import onvif_client
    monkeypatch.setattr(onvif_client, "_is_onvif_device", _fake_probe(set()))
    assert scan_subnet(private, ports=(80,)) == []


def test_oversized_range_rejected():
    """/16 — это 65534 адреса: почти наверняка опечатка в маске, а не
    намерение, и превратило бы кнопку в многочасовой перебор."""
    with pytest.raises(OnvifError, match=str(MAX_SCAN_HOSTS)):
        scan_subnet("10.0.0.0/16")


def test_ipv6_rejected():
    with pytest.raises(OnvifError, match="IPv4"):
        scan_subnet("fd00::/120")
