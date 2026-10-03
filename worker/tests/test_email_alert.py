"""Веерная рассылка watchlist-алерта по каналам §6 (Telegram, email, звук).

Главное, что здесь проверяется, — кулдаун снимается ОДИН раз на событие и
общий для каналов. Наивная реализация («каждый канал берёт свой
`r.set(..., nx=True)`») выглядит правильной и молча ломает почту при
включённом Telegram: первый канал забирает ключ, второй видит занятый и
выходит. Проявилось бы только на объекте с обоими включёнными каналами.

Требует полный requirements.txt воркера (см. докстринг
test_camera_worker_onvif_thread.py) — pytest.importorskip как и там.
"""
import os
import socket

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.), см. докстринг модуля"
)


class _FakeRedis:
    """SET nx=True с настоящей семантикой: второй раз по тому же ключу — None."""

    def __init__(self):
        self.keys = {}
        self.calls = 0

    def set(self, key, value, ex=None, nx=False):
        self.calls += 1
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(worker, "r", fake)
    return fake


@pytest.fixture()
def channels(monkeypatch):
    """Подменяет оба канала счётчиками вызовов."""
    calls = {"telegram": [], "email": []}
    monkeypatch.setattr(worker, "send_telegram_alert",
                        lambda text, pid=None, cam=None: calls["telegram"].append(text))
    monkeypatch.setattr(worker, "send_email_alert",
                        lambda subj, text, pid=None, cam=None: calls["email"].append((subj, text)))
    return calls


def test_both_channels_fire_for_one_event(fake_redis, channels):
    worker.send_person_alert(7, "Иванов", 3, "")
    assert len(channels["telegram"]) == 1
    assert len(channels["email"]) == 1
    assert "Иванов" in channels["telegram"][0]
    assert "Иванов" in channels["email"][0][1]


def test_cooldown_is_taken_once_and_shared(fake_redis, channels):
    """Регрессия: один `r.set(nx=True)` на событие, а не по одному на канал.

    Проверяется и число обращений к Redis (один на событие), и то, что оба
    канала сработали — при кулдауне на канал почта получила бы ноль писем.
    """
    worker.send_person_alert(7, "Иванов", 3, "")
    assert fake_redis.calls == 1
    assert len(channels["telegram"]) == len(channels["email"]) == 1


def test_second_event_within_cooldown_is_silent_on_all_channels(fake_redis, channels):
    worker.send_person_alert(7, "Иванов", 3, "")
    worker.send_person_alert(7, "Иванов", 3, "")
    assert len(channels["telegram"]) == 1
    assert len(channels["email"]) == 1


def test_cooldown_is_per_person(fake_redis, channels):
    """Разные персоны — независимые алерты: кулдаун §6 защищает от повтора
    одного человека, а не от второго человека в кадре."""
    worker.send_person_alert(7, "Иванов", 3, "")
    worker.send_person_alert(8, "Петров", 3, "")
    assert len(channels["email"]) == 2


def test_redis_outage_does_not_silence_alerts(monkeypatch, channels):
    """Недоступный Redis не должен глушить оповещения: кулдаун — защита от
    спама, а не условие доставки."""
    class _Broken:
        def set(self, *a, **kw):
            raise ConnectionError("redis down")

    monkeypatch.setattr(worker, "r", _Broken())
    worker.send_person_alert(7, "Иванов", 3, "")
    assert len(channels["telegram"]) == len(channels["email"]) == 1


def test_email_channel_failure_does_not_stop_telegram(fake_redis, monkeypatch):
    """Каналы независимы: отказ SMTP не должен отменять Telegram и наоборот.
    Иначе один неверный пароль от почты выключил бы алерты целиком."""
    sent = []
    monkeypatch.setattr(worker, "send_telegram_alert",
                        lambda text, pid=None, cam=None: sent.append(text))
    monkeypatch.setattr(worker.mailer, "send_email",
                        lambda *a, **kw: (_ for _ in ()).throw(worker.mailer.MailerError("SMTP: отказ")))
    worker.send_person_alert(7, "Иванов", 3, "")
    assert len(sent) == 1  # Telegram ушёл, несмотря на падение почты


def test_email_alert_reports_failure_without_leaking_password(fake_redis, monkeypatch, caplog):
    """Пароль SMTP не должен попадать в лог — журнал уезжает в systemd и
    в файлы ротации, доступные шире, чем таблица settings."""
    monkeypatch.setitem(worker.CONFIG, "smtp_host", "127.0.0.1")
    monkeypatch.setitem(worker.CONFIG, "smtp_port", _free_port())
    monkeypatch.setitem(worker.CONFIG, "smtp_user", "nvr@object.local")
    monkeypatch.setitem(worker.CONFIG, "smtp_password", "s3cr3t-mail-pass")
    monkeypatch.setitem(worker.CONFIG, "smtp_tls", "none")
    monkeypatch.setitem(worker.CONFIG, "alert_email_to", "guard@object.local")
    with caplog.at_level("WARNING"):
        assert worker.send_email_alert("Тема", "Текст") is False
    assert "s3cr3t-mail-pass" not in caplog.text


def test_email_alert_returns_false_when_not_configured(fake_redis, monkeypatch):
    monkeypatch.setitem(worker.CONFIG, "smtp_host", "")
    assert worker.send_email_alert("Тема", "Текст") is False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
