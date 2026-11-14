"""§9 «Алерты: потеря потока, пропуск записи, переполнение диска».

Из трёх алертов раздела оповещение уходило только по диску. Потеря потока и
пропуск записи писались в лог и красились в интерфейсе «Мониторинга» — то
есть доходили лишь до того, кто в эту минуту смотрит на страницу. Довод,
по которому диску оповещение сделали, записан прямо в коде цикла 29:
«журнал на объекте никто не читает, пока архив не начал стираться». К этим
двум он приложим сильнее: камера, переставшая писаться ночью, не оставляет
по себе ничего, кроме дыры в архиве, и находят её в день, когда запись
понадобилась.

Проверяется поведение, ломающееся молча:

* пачка — одно сообщение, а не сто двадцать (иначе алертинг выключают, и
  следующий, настоящий, никто не читает);
* кулдаун **на камеру**, а не общий: вторая отвалившаяся камера — это
  развитие аварии, ради которого алерт и заведён;
* «пропуск записи» уровневый, а не по переходу: без кулдауна он бы слал
  сообщение каждые десять секунд, пока камеру не починят;
* отказ Redis не глушит оповещение.
"""
import os
import types

import pytest

# Тот же порядок, что в test_retention_rotation.py: DATABASE_URL до импорта
# (worker.py читает его на уровне модуля), тяжёлый импорт — через
# importorskip, чтобы лёгкая CI-джоба воркера аккуратно пропускала набор,
# а не падала на cv2 (урок цикла 16).
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)


@pytest.fixture()
def sent(monkeypatch):
    """Перехват обоих каналов. Нить отправки заменяется синхронным вызовом:
    иначе тест гонялся бы с фоновым потоком и падал бы через раз."""
    calls = []
    monkeypatch.setattr(worker, "send_telegram_alert",
                        lambda text, *a, **k: calls.append(("tg", text)))
    monkeypatch.setattr(worker, "send_email_alert",
                        lambda subject, text, *a, **k: calls.append(("mail", subject, text)))

    class _Now(types.SimpleNamespace):
        def __init__(self, target, args=(), daemon=False):
            target(*args)

        def start(self):
            pass

    monkeypatch.setattr(worker.threading, "Thread", _Now)
    return calls


@pytest.fixture()
def open_cooldown(monkeypatch):
    """Кулдаун истёк по любой камере."""
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)


def test_mass_outage_sends_one_message_not_one_per_camera(sent, open_cooldown):
    cams = list(range(1, 121))

    reported = worker.send_record_layer_alert("stream_lost", cams)

    assert reported == cams
    assert len(sent) == 2, "ожидались ровно два сообщения — Telegram и почта"
    body = sent[0][1]
    assert "камер 120" in body
    # Список усечён: письмо со ста двадцатью номерами нечитаемо, а Telegram
    # его просто обрежет по своему пределу длины.
    assert "и ещё 100" in body
    assert "#1," in body and "#20" in body


def test_cooldown_is_per_camera_not_per_kind(sent, monkeypatch):
    """Вторая камера, отвалившаяся следом, обязана дать своё сообщение."""
    seen = set()

    def _set(key, *a, **k):
        if key in seen:
            return None
        seen.add(key)
        return True

    monkeypatch.setattr(worker.r, "set", _set)

    assert worker.send_record_layer_alert("stream_lost", [7]) == [7]
    # Та же камера в кулдауне — молчим.
    assert worker.send_record_layer_alert("stream_lost", [7]) == []
    # Другая камера — сообщение уходит.
    assert worker.send_record_layer_alert("stream_lost", [8]) == [8]
    assert len(sent) == 4, "два события × два канала"


def test_kinds_do_not_share_cooldown(sent, monkeypatch):
    """Потеря потока и пропуск записи — разные события одной камеры.

    Общий ключ означал бы, что камера, сначала потерявшая поток, а затем
    переставшая писаться, отчитается только о первом.
    """
    seen = set()

    def _set(key, *a, **k):
        if key in seen:
            return None
        seen.add(key)
        return True

    monkeypatch.setattr(worker.r, "set", _set)

    assert worker.send_record_layer_alert("stream_lost", [5]) == [5]
    assert worker.send_record_layer_alert("segment_missing", [5]) == [5]


def test_repeated_gap_is_silenced_until_cooldown_expires(sent, monkeypatch):
    """`segment_gaps()` уровневый: возвращает пропуск на КАЖДОМ проходе.

    Без кулдауна одна невосстановленная камера слала бы сообщение каждые
    десять секунд до самого ремонта.
    """
    seen = set()

    def _set(key, *a, **k):
        if key in seen:
            return None
        seen.add(key)
        return True

    monkeypatch.setattr(worker.r, "set", _set)

    assert worker.send_record_layer_alert("segment_missing", [3]) == [3]
    for _ in range(10):
        assert worker.send_record_layer_alert("segment_missing", [3]) == []
    assert len(sent) == 2


def test_alert_survives_redis_failure(sent, monkeypatch):
    """Молчащий алертинг хуже повторов — как и у диска."""
    def _boom(*a, **k):
        raise RuntimeError("redis недоступен")

    monkeypatch.setattr(worker.r, "set", _boom)

    assert worker.send_record_layer_alert("stream_lost", [1]) == [1]
    assert len(sent) == 2


def test_both_channels_are_used(sent, open_cooldown):
    """§11 настраивает Telegram и почту независимо; администратор,
    включивший оба, ждёт дублирования, а не «какого-нибудь»."""
    worker.send_record_layer_alert("segment_missing", [2])

    assert {c[0] for c in sent} == {"tg", "mail"}
    subject = next(c[1] for c in sent if c[0] == "mail")
    assert "запись не идёт" in subject.lower()


def test_nothing_is_sent_without_cameras(sent, open_cooldown):
    assert worker.send_record_layer_alert("stream_lost", []) == []
    assert sent == []
