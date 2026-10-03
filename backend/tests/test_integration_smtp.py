"""Сквозная проверка почтовых уведомлений (SPEC §11, §6, §8) на НАСТОЯЩЕМ
SMTP-сервере, а не на моке smtplib.

Мок здесь ничего не доказывал бы: класс ошибок, ради которого функция
существует (не тот порт, STARTTLS против SSL, отвергнутый получатель,
письмо без Date/Message-ID), проявляется именно во взаимодействии с
сервером. `aiosmtpd` поднимается в том же процессе на свободном порту —
внешняя сеть не нужна, в CI работает так же, как в песочнице.
"""
import asyncio
import socket
import threading
import time

import pytest

pytest.importorskip("aiosmtpd", reason="aiosmtpd не установлен")

from aiosmtpd.controller import Controller  # noqa: E402

from app.services.mailer import (  # noqa: E402
    MailerError,
    build_message,
    send_email,
    split_recipients,
)


class _Collector:
    """Handler aiosmtpd: складывает принятые письма и, по требованию,
    отвергает получателя — так проверяется ветка SMTPRecipientsRefused."""

    def __init__(self, refuse: str | None = None):
        self.messages: list[tuple[str, list[str], bytes]] = []
        self.refuse = refuse

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        if self.refuse and address == self.refuse:
            return "550 no such user"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append(
            (envelope.mail_from, list(envelope.rcpt_tos), envelope.content)
        )
        return "250 Message accepted"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def smtpd():
    """Живой SMTP на localhost. Controller крутит свой event loop в отдельном
    потоке, поэтому не конфликтует с loop'ом TestClient'а."""
    handler = _Collector()
    controller = Controller(handler, hostname="127.0.0.1", port=_free_port())
    controller.start()
    try:
        yield controller, handler
    finally:
        controller.stop()


# --- Транспорт ---------------------------------------------------------------

def test_email_reaches_the_server(smtpd):
    controller, handler = smtpd
    sent = send_email(
        controller.hostname, controller.port, "", "", "none",
        "nvr@object.local", "guard@object.local", "Тема", "Тело письма",
    )
    assert sent is True
    assert len(handler.messages) == 1
    mail_from, rcpt_tos, content = handler.messages[0]
    assert mail_from == "nvr@object.local"
    assert rcpt_tos == ["guard@object.local"]
    # Кириллица должна доезжать читаемой. На проводе заголовок обязан быть
    # закодирован по RFC 2047 (`=?utf-8?b?...?=`) — проверяем, что почтовый
    # клиент развернёт его обратно в исходную строку, разбирая письмо тем же
    # policy.default, что и любой современный MUA.
    import email
    import email.policy

    parsed = email.message_from_bytes(content, policy=email.policy.default)
    assert parsed["Subject"] == "Тема"
    assert b"=?utf-8?" in content  # тема действительно ушла закодированной
    assert "Тело письма" in parsed.get_payload(decode=True).decode()
    # Без Date/Message-ID часть релеев кладёт письмо в спам (см. build_message)
    assert parsed["Date"]
    assert parsed["Message-ID"]


def test_all_recipients_get_the_letter(smtpd):
    controller, handler = smtpd
    send_email(
        controller.hostname, controller.port, "", "", "none", "nvr@object.local",
        "a@object.local, b@object.local; c@object.local", "Тема", "Тело",
    )
    assert handler.messages[0][1] == ["a@object.local", "b@object.local", "c@object.local"]


def test_missing_host_is_not_an_error(smtpd):
    """Незаполненная форма — это «почта выключена», а не отказ: иначе
    алерт-путь воркера писал бы в лог ошибку на каждом событии."""
    assert send_email("", 25, "", "", "none", "a@b.c", "d@e.f", "s", "b") is False


def test_missing_recipients_is_not_an_error(smtpd):
    controller, _ = smtpd
    assert send_email(controller.hostname, controller.port, "", "", "none",
                      "a@b.c", "   ", "s", "b") is False


def test_refused_recipient_raises_mailer_error():
    handler = _Collector(refuse="ghost@object.local")
    controller = Controller(handler, hostname="127.0.0.1", port=_free_port())
    controller.start()
    try:
        with pytest.raises(MailerError) as e:
            send_email(controller.hostname, controller.port, "", "", "none",
                       "nvr@object.local", "ghost@object.local", "s", "b")
        assert "получател" in str(e.value)
    finally:
        controller.stop()


def test_closed_port_raises_mailer_error_not_oserror():
    """Вызывающий ловит один тип исключения. Голый OSError из smtplib
    пробивал бы обработчик роутера и превращался бы в 500 вместо 502."""
    port = _free_port()  # никто не слушает
    with pytest.raises(MailerError) as e:
        send_email("127.0.0.1", port, "", "", "none", "a@b.c", "d@e.f", "s", "b")
    assert "SMTP" in str(e.value)


def test_send_does_not_hang_forever_on_silent_server():
    """Сокет, который принимает соединение и молчит: без таймаута отправка
    удерживала бы поток бэкенда/воркера бесконечно."""
    from app.services import mailer

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def _accept_and_stay_silent():
        try:
            conn, _ = srv.accept()
            while not stop.wait(0.05):
                pass
            conn.close()
        except OSError:
            pass

    t = threading.Thread(target=_accept_and_stay_silent, daemon=True)
    t.start()
    original = mailer.SMTP_TIMEOUT_SEC
    mailer.SMTP_TIMEOUT_SEC = 1  # держать тест 15 секунд незачем
    started = time.monotonic()
    try:
        with pytest.raises(MailerError):
            send_email("127.0.0.1", port, "", "", "none", "a@b.c", "d@e.f", "s", "b")
    finally:
        mailer.SMTP_TIMEOUT_SEC = original
        stop.set()
        srv.close()
    assert time.monotonic() - started < 10


def test_attachment_survives_roundtrip(smtpd):
    """§8 шлёт отчёт вложением — проверяем, что байты доезжают без порчи."""
    controller, handler = smtpd
    payload = b"\x50\x4b\x03\x04binary\x00\xff report"
    msg = build_message("nvr@object.local", ["guard@object.local"], "Отчёт", "Во вложении",
                        [("report.xlsx", payload, "octet-stream")])
    from app.services.mailer import send_message

    send_message(msg, controller.hostname, controller.port, "", "", "none")
    import email

    parsed = email.message_from_bytes(handler.messages[0][2])
    attachments = [p for p in parsed.walk() if p.get_filename()]
    assert [p.get_filename() for p in attachments] == ["report.xlsx"]
    assert attachments[0].get_payload(decode=True) == payload


@pytest.mark.parametrize("raw,expected", [
    ("a@b.c", ["a@b.c"]),
    ("a@b.c, a@b.c", ["a@b.c"]),                 # дубли схлопываются
    (" a@b.c ;\n b@c.d ", ["a@b.c", "b@c.d"]),
    ("", []),
    ("  ,  ; ", []),
])
def test_split_recipients(raw, expected):
    assert split_recipients(raw) == expected


# --- Эндпоинты ---------------------------------------------------------------

def test_test_email_endpoint_sends_through_configured_server(client, admin_headers, smtpd):
    controller, handler = smtpd
    saved = client.get("/api/settings", headers=admin_headers).json()
    try:
        r = client.put("/api/settings", headers=admin_headers, json={
            "smtp_host": controller.hostname,
            "smtp_port": controller.port,
            "smtp_tls": "none",
            "smtp_from": "nvr@object.local",
            "alert_email_to": "guard@object.local, chief@object.local",
        })
        assert r.status_code == 200, r.text
        r = client.post("/api/settings/test-email", headers=admin_headers)
        assert r.status_code == 200, r.text
        assert r.json()["recipients"] == ["guard@object.local", "chief@object.local"]
        assert len(handler.messages) == 1
        assert handler.messages[0][1] == ["guard@object.local", "chief@object.local"]
    finally:
        client.put("/api/settings", headers=admin_headers, json={
            "smtp_host": saved.get("smtp_host", ""),
            "smtp_port": int(saved.get("smtp_port") or 587),
            "smtp_tls": saved.get("smtp_tls") or "starttls",
            "smtp_from": saved.get("smtp_from", ""),
            "alert_email_to": saved.get("alert_email_to", ""),
        })


def test_test_email_reports_unreachable_server_as_502(client, admin_headers):
    saved = client.get("/api/settings", headers=admin_headers).json()
    try:
        client.put("/api/settings", headers=admin_headers, json={
            "smtp_host": "127.0.0.1", "smtp_port": _free_port(),
            "smtp_tls": "none", "alert_email_to": "guard@object.local",
        })
        r = client.post("/api/settings/test-email", headers=admin_headers)
        # 502, а не 500: отказ внешнего релея — не отказ бэкенда, и текст
        # должен объяснять администратору, что чинить.
        assert r.status_code == 502, r.text
        assert "SMTP" in r.json()["detail"]
    finally:
        client.put("/api/settings", headers=admin_headers, json={
            "smtp_host": saved.get("smtp_host", ""),
            "smtp_port": int(saved.get("smtp_port") or 587),
            "smtp_tls": saved.get("smtp_tls") or "starttls",
            "alert_email_to": saved.get("alert_email_to", ""),
        })


def test_test_email_without_host_is_400(client, admin_headers):
    r = client.post("/api/settings/test-email", headers=admin_headers)
    assert r.status_code == 400
    assert "smtp_host" in r.json()["detail"]


def test_smtp_tls_mode_is_validated(client, admin_headers):
    r = client.put("/api/settings", headers=admin_headers, json={"smtp_tls": "tls-please"})
    assert r.status_code == 400, r.text


def test_smtp_password_is_encrypted_at_rest(client, admin_headers, pg_conn):
    """§14: секреты в БД только зашифрованными. Проверяется сама строка в
    таблице, а не ответ API — именно она уезжает в дампы pg_dump."""
    from app.services.encryption import SECRET_SETTING_PREFIX

    secret = "s3cr3t-mail-pass"
    saved = client.get("/api/settings", headers=admin_headers).json()
    try:
        assert client.put("/api/settings", headers=admin_headers,
                          json={"smtp_password": secret}).status_code == 200
        with pg_conn.cursor() as cur:
            cur.execute("SELECT value FROM settings WHERE key = 'smtp_password'")
            stored = cur.fetchone()[0]
        assert stored.startswith(SECRET_SETTING_PREFIX)
        assert secret not in stored
        # Админка должна видеть расшифрованное значение, иначе первое же
        # сохранение формы затёрло бы пароль маской.
        assert client.get("/api/settings", headers=admin_headers).json()["smtp_password"] == secret
    finally:
        client.put("/api/settings", headers=admin_headers,
                   json={"smtp_password": saved.get("smtp_password", "")})


# --- /api/settings/client ----------------------------------------------------

def test_client_settings_never_expose_secrets(client, admin_headers):
    from app.routers.settings import CLIENT_SETTING_KEYS
    from app.services.encryption import SECRET_SETTING_KEYS

    r = client.get("/api/settings/client", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert set(r.json()) == set(CLIENT_SETTING_KEYS)
    assert not (set(r.json()) & SECRET_SETTING_KEYS)


def test_client_settings_readable_by_viewer(client, make_user, request):
    """§18: «Просмотр видео онлайн» доступен наблюдателю, значит и звуковой
    алерт §6 на Стене — тоже. Полный GET /api/settings ему по-прежнему
    закрыт: он отдаёт расшифрованные секреты."""
    _, token = make_user(f"viewer_{request.node.name}"[:32], "viewer")
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/settings/client", headers=headers).status_code == 200
    assert client.get("/api/settings", headers=headers).status_code == 403


def test_client_settings_require_authentication(client):
    assert client.get("/api/settings/client").status_code == 401
