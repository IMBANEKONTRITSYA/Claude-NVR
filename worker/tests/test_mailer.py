"""Копия SMTP-транспорта в воркере — на живом SMTP-сервере (SPEC §6).

Модуль на одном stdlib, поэтому тест НЕ требует полного requirements.txt
воркера (cv2/insightface) и в CI выполняется, а не скипается — в отличие от
test_email_alert.py, который импортирует worker.py целиком.

Поведение сверяется здесь заново, а не «доверяется» бэкендовой копии:
backend/tests/test_mailer_parity.py доказывает идентичность AST, но обе
копии могут быть сломаны одинаково, а этот тест гоняет именно тот файл,
который поедет в контейнер воркера.
"""
import email
import email.policy
import socket

import pytest

pytest.importorskip("aiosmtpd", reason="aiosmtpd не установлен")

from aiosmtpd.controller import Controller  # noqa: E402

import mailer  # noqa: E402


class _Collector:
    def __init__(self):
        self.messages = []

    async def handle_RCPT(self, server, session, envelope, address, rcpt_options):
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server, session, envelope):
        self.messages.append((envelope.mail_from, list(envelope.rcpt_tos), envelope.content))
        return "250 Message accepted"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def smtpd():
    handler = _Collector()
    controller = Controller(handler, hostname="127.0.0.1", port=_free_port())
    controller.start()
    try:
        yield controller, handler
    finally:
        controller.stop()


def test_worker_copy_delivers_mail(smtpd):
    controller, handler = smtpd
    sent = mailer.send_email(
        controller.hostname, controller.port, "", "", "none",
        "nvr@object.local", "guard@object.local",
        "FaceWatch: обнаружена персона", "Персона «Иванов» на камере #3",
    )
    assert sent is True
    parsed = email.message_from_bytes(handler.messages[0][2], policy=email.policy.default)
    assert parsed["Subject"] == "FaceWatch: обнаружена персона"
    assert "Иванов" in parsed.get_payload(decode=True).decode()


def test_worker_copy_is_silent_when_mail_is_not_configured(smtpd):
    """Пустой smtp_host — почтовый канал просто выключен. Если бы это была
    ошибка, воркер писал бы в лог на каждом watchlist-событии."""
    assert mailer.send_email("", 587, "", "", "starttls", "", "a@b.c", "s", "b") is False


def test_worker_copy_raises_typed_error_on_dead_relay():
    with pytest.raises(mailer.MailerError):
        mailer.send_email("127.0.0.1", _free_port(), "", "", "none",
                          "a@b.c", "d@e.f", "s", "b")


def test_unknown_tls_mode_falls_back_to_starttls_not_crash():
    """Настройка могла приехать из БД, развёрнутой другой версией. Падение
    здесь означало бы, что нить алерта умирает молча."""
    with pytest.raises(mailer.MailerError):
        # starttls на сервере, которого нет → MailerError, а не ValueError
        mailer.send_email("127.0.0.1", _free_port(), "", "", "нет-такого-режима",
                          "a@b.c", "d@e.f", "s", "b")
