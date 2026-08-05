"""Очистка вывода диагностики от учётных данных.

Смысл инструмента в том, что его вывод можно передать целиком, не вычищая
руками. Если очистка пропустит пароль хоть в одном из мест, где он
появляется, пользователь опубликует его, будучи уверенным в обратном, —
поэтому проверяется каждое такое место.
"""
import pytest

pytest.importorskip("defusedxml", reason="ONVIF-клиент требует defusedxml")

from onvif_dump import redact  # noqa: E402

USER = "admin"
PW = "admin123456!"


def test_password_in_plain_text_is_removed():
    assert PW not in redact(f"пароль {PW} внутри текста", USER, PW)


def test_username_is_removed():
    assert "admin" not in redact("<Username>admin</Username>", USER, PW)


def test_credentials_inside_uri_are_removed():
    out = redact(f"rtsp://{USER}:{PW}@192.168.105.19/media/video1", USER, PW)
    assert PW not in out and "***" in out
    assert "192.168.105.19" in out, "адрес камеры нужен для разбора, он не секрет"


def test_percent_encoded_credentials_in_uri_are_removed():
    """inject_credentials кодирует пароль, поэтому буквального совпадения
    может не быть — на этот случай работает правило для user:pass@host."""
    out = redact("rtsp://admin:admin123456%21@192.168.105.19/v", USER, PW)
    assert "admin123456%21" not in out
    assert "***:***@192.168.105.19" in out


def test_password_digest_and_nonce_are_removed():
    """Дайджест и nonce выводятся от пароля — публиковать их незачем."""
    xml = ('<Password Type="...#PasswordDigest">R2Rv0k9xQ=</Password>'
           '<Nonce EncodingType="...">bm9uY2U=</Nonce>')
    out = redact(xml, USER, PW)
    assert "R2Rv0k9xQ=" not in out
    assert "bm9uY2U=" not in out


def test_survives_missing_credentials():
    """Камера может быть без пароля — очистка не должна падать на None."""
    assert redact("rtsp://192.168.105.19/v", None, None) == "rtsp://192.168.105.19/v"


def test_useful_content_is_preserved():
    """Смысл вывода — разрешения и токены профилей: их вычищать нельзя,
    иначе фикстура станет бесполезной."""
    xml = "<Profiles token='profile_1'><Width>1920</Width><Height>1080</Height></Profiles>"
    assert redact(xml, USER, PW) == xml
