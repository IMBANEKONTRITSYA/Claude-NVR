"""Шаблоны отчётов и авто-отправка (SPEC §8) сквозь весь путь: API →
Postgres → построение файла → SMTP.

Отдельно от test_report_scheduler.py: там чистая логика слотов, здесь —
что отчёт действительно доезжает письмом с вложением, которое открывается
как Excel/CSV. Ни то, ни другое по отдельности не доказывает, что функция
работает: слот может быть посчитан верно, а вложение уехать пустым.
"""
import io
import socket
import zipfile
from datetime import datetime, timedelta

import pytest

pytest.importorskip("aiosmtpd", reason="aiosmtpd не установлен")

from aiosmtpd.controller import Controller  # noqa: E402


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


@pytest.fixture()
def mail_settings(client, admin_headers, smtpd):
    """Настраивает почту на локальный SMTP и возвращает прежние значения."""
    controller, handler = smtpd
    saved = client.get("/api/settings", headers=admin_headers).json()
    client.put("/api/settings", headers=admin_headers, json={
        "smtp_host": controller.hostname, "smtp_port": controller.port,
        "smtp_tls": "none", "smtp_from": "nvr@object.local",
        "alert_email_to": "guard@object.local",
    })
    yield handler
    client.put("/api/settings", headers=admin_headers, json={
        "smtp_host": saved.get("smtp_host", ""),
        "smtp_port": int(saved.get("smtp_port") or 587),
        "smtp_tls": saved.get("smtp_tls") or "starttls",
        "smtp_from": saved.get("smtp_from", ""),
        "alert_email_to": saved.get("alert_email_to", ""),
    })


@pytest.fixture()
def make_schedule(client, admin_headers):
    """Заводит шаблон и убирает его за собой (см. докстринг make_user)."""
    created = []

    def _make(**over):
        payload = {"name": "Ночной отчёт", "kind": "appearances", "fmt": "xlsx",
                   "days": 7, "recipients": "chief@object.local"}
        payload.update(over)
        r = client.post("/api/reports/schedules", json=payload, headers=admin_headers)
        assert r.status_code == 200, r.text
        created.append(r.json()["id"])
        return r.json()

    yield _make
    for sid in created:
        client.delete(f"/api/reports/schedules/{sid}", headers=admin_headers)


# --- CRUD --------------------------------------------------------------------

def test_schedule_roundtrip(client, admin_headers, make_schedule):
    made = make_schedule(name="Еженедельный", period="weekly", day_of_week=2, hour=9)
    got = [s for s in client.get("/api/reports/schedules", headers=admin_headers).json()
           if s["id"] == made["id"]]
    assert got and got[0]["name"] == "Еженедельный"
    assert got[0]["period"] == "weekly" and got[0]["day_of_week"] == 2


def test_unknown_kind_is_rejected(client, admin_headers):
    r = client.post("/api/reports/schedules", headers=admin_headers,
                    json={"name": "x", "kind": "нет-такого", "fmt": "csv"})
    assert r.status_code == 400


def test_unknown_period_is_rejected(client, admin_headers):
    r = client.post("/api/reports/schedules", headers=admin_headers,
                    json={"name": "x", "kind": "appearances", "period": "hourly"})
    assert r.status_code == 400


def test_day_of_month_above_28_is_rejected(client, admin_headers):
    """«31-го числа» молча не сработало бы в феврале — отказ на входе."""
    r = client.post("/api/reports/schedules", headers=admin_headers,
                    json={"name": "x", "kind": "appearances", "period": "monthly",
                          "day_of_month": 31})
    assert r.status_code == 422


def test_editing_a_schedule_reopens_the_slot(client, admin_headers, make_schedule, pg_conn):
    """Сдвинув время с 20:00 на 08:00, администратор ждёт отчёт завтра в 8,
    а не «уже отправляли сегодня»."""
    made = make_schedule(hour=20)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE report_schedules SET last_sent_at = now() WHERE id = %s",
                    (made["id"],))
    r = client.put(f"/api/reports/schedules/{made['id']}", headers=admin_headers,
                   json={"name": made["name"], "kind": "appearances", "fmt": "xlsx",
                         "days": 7, "recipients": "chief@object.local", "hour": 8})
    assert r.status_code == 200, r.text
    assert r.json()["last_sent_at"] is None


def test_viewer_cannot_manage_schedules(client, make_user, request):
    """§18: отчёты — «Админ да, Оператор да, Наблюдатель нет»."""
    _, token = make_user(f"v_{abs(hash(request.node.name)) % 10**6}", "viewer")
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/reports/schedules", headers=h).status_code == 403
    assert client.post("/api/reports/schedules", headers=h,
                       json={"name": "x", "kind": "appearances"}).status_code == 403


def test_operator_can_read_but_not_create(client, make_user, request):
    """Создание рассылки меняет, какие данные уезжают с объекта, — это
    настройка (§18: «Управление пользователями/настройки» — только админ)."""
    _, token = make_user(f"o_{abs(hash(request.node.name)) % 10**6}", "operator")
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/reports/schedules", headers=h).status_code == 200
    assert client.post("/api/reports/schedules", headers=h,
                       json={"name": "x", "kind": "appearances"}).status_code == 403


# --- отправка ----------------------------------------------------------------

def _attachments(raw: bytes):
    import email
    import email.policy

    parsed = email.message_from_bytes(raw, policy=email.policy.default)
    return [(p.get_filename(), p.get_payload(decode=True))
            for p in parsed.walk() if p.get_filename()]


def test_send_now_delivers_xlsx_attachment(client, admin_headers, make_schedule, mail_settings):
    made = make_schedule(fmt="xlsx")
    r = client.post(f"/api/reports/schedules/{made['id']}/send", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert len(mail_settings.messages) == 1
    mail_from, rcpt, raw = mail_settings.messages[0]
    assert rcpt == ["chief@object.local"]
    files = _attachments(raw)
    assert [n for n, _ in files] == ["appearances.xlsx"]
    # Вложение должно открываться как настоящий xlsx, а не быть пустым
    # набором байт с правильным именем: zip-контейнер с worksheet внутри.
    with zipfile.ZipFile(io.BytesIO(files[0][1])) as z:
        assert any(n.startswith("xl/worksheets/") for n in z.namelist())


def test_send_now_delivers_csv_attachment(client, admin_headers, make_schedule, mail_settings):
    made = make_schedule(fmt="csv", kind="cameras")
    assert client.post(f"/api/reports/schedules/{made['id']}/send",
                       headers=admin_headers).status_code == 200
    files = _attachments(mail_settings.messages[0][2])
    assert files[0][0] == "cameras.csv"
    assert "Камера" in files[0][1].decode("utf-8")
    # Отчёт, пришедший почтой, открывают тем же Excel, что и скачанный:
    # без BOM его кириллические заголовки читаются как cp1251 (см.
    # services/csv_export.py). Проверка байтов, а не декодированного
    # текста, — `.decode("utf-8")` выше проходит при обоих исходах.
    assert files[0][1].startswith(b"\xef\xbb\xbf"), (
        f"вложение §8 ушло без BOM: {files[0][1][:20]!r}"
    )


def test_send_now_falls_back_to_alert_recipients(client, admin_headers, make_schedule, mail_settings):
    """Пустое поле получателей — не ошибка: отчёт идёт тем же людям, что и
    алерты, чтобы не вводить адреса второй раз."""
    made = make_schedule(recipients="")
    assert client.post(f"/api/reports/schedules/{made['id']}/send",
                       headers=admin_headers).status_code == 200
    assert mail_settings.messages[0][1] == ["guard@object.local"]


def test_send_now_without_smtp_is_400(client, admin_headers, make_schedule):
    made = make_schedule()
    r = client.post(f"/api/reports/schedules/{made['id']}/send", headers=admin_headers)
    assert r.status_code == 400
    assert "очта" in r.json()["detail"]


def test_send_now_does_not_close_the_scheduled_slot(client, admin_headers,
                                                    make_schedule, mail_settings, pg_conn):
    """Проверка кнопкой не должна отменять утренний отчёт."""
    made = make_schedule(enabled=True)
    client.post(f"/api/reports/schedules/{made['id']}/send", headers=admin_headers)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_sent_at FROM report_schedules WHERE id = %s", (made["id"],))
        assert cur.fetchone()[0] is None


# --- проход планировщика на живой БД ----------------------------------------

def test_run_due_sends_and_closes_the_slot(client, admin_headers, make_schedule,
                                           mail_settings, pg_conn):
    """Полный проход: расписание в БД → построение → письмо → слот закрыт
    → повторный проход молчит."""

    made = make_schedule(enabled=True, period="daily", hour=8, fmt="csv")
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)

    # run_due открывает собственную сессию через SessionLocal, поэтому
    # гоняется в своём event loop — как и любой код, не привязанный к
    # loop'у TestClient'а (см. докстринг conftest.client).
    assert run_due_now(now) == 1
    assert len(mail_settings.messages) == 1

    with pg_conn.cursor() as cur:
        cur.execute("SELECT last_sent_at FROM report_schedules WHERE id = %s", (made["id"],))
        last = cur.fetchone()[0]
    assert last is not None and last.hour == 8 and last.minute == 0

    # Второй проход в тот же слот — ни одного письма
    assert run_due_now(now) == 0
    assert len(mail_settings.messages) == 1

    # Следующие сутки — снова отправка
    assert run_due_now(now + timedelta(days=1)) == 1
    assert len(mail_settings.messages) == 2


def test_run_due_records_error_and_keeps_slot_open(client, admin_headers,
                                                   make_schedule, pg_conn):
    """Недоступный релей: причина видна в интерфейсе, слот НЕ закрывается —
    иначе одна сетевая ошибка отменяла бы отчёт до следующего дня."""
    saved = client.get("/api/settings", headers=admin_headers).json()
    client.put("/api/settings", headers=admin_headers, json={
        "smtp_host": "127.0.0.1", "smtp_port": _free_port(), "smtp_tls": "none",
        "alert_email_to": "guard@object.local",
    })
    try:
        made = make_schedule(enabled=True, hour=8)
        now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
        assert run_due_now(now) == 0
        with pg_conn.cursor() as cur:
            cur.execute("SELECT last_sent_at, last_error FROM report_schedules WHERE id = %s",
                        (made["id"],))
            last_sent, last_error = cur.fetchone()
        assert last_sent is None, "слот закрылся, несмотря на неудачу"
        assert last_error and "SMTP" in last_error
    finally:
        client.put("/api/settings", headers=admin_headers, json={
            "smtp_host": saved.get("smtp_host", ""),
            "smtp_port": int(saved.get("smtp_port") or 587),
            "smtp_tls": saved.get("smtp_tls") or "starttls",
            "alert_email_to": saved.get("alert_email_to", ""),
        })


def test_disabled_schedule_is_not_sent(client, admin_headers, make_schedule, mail_settings):
    make_schedule(enabled=False, hour=8)
    now = datetime.now().replace(hour=9, minute=0, second=0, microsecond=0)
    assert run_due_now(now) == 0
    assert mail_settings.messages == []


def run_due_now(now):
    """Прогоняет проход планировщика в отдельном потоке и event loop'е.

    Два ограничения, из-за которых нельзя проще. `asyncio.run` прямо в
    тесте нельзя: у TestClient'а свой работающий loop, и вложенный запуск
    в том же потоке ломает пул asyncpg, общий с сессионной фикстурой (см.
    tests/conftest.py). И собственный пул приложения тоже нельзя — он
    привязан к loop'у TestClient'а, — поэтому проход получает свой engine,
    созданный здесь же, в рабочем loop'е, и аккуратно закрываемый после.
    """
    import asyncio
    import threading

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.config import settings
    from app.services.report_scheduler import run_due

    box = {}

    async def _go():
        engine = create_async_engine(settings.DATABASE_URL, poolclass=None)
        maker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            return await run_due(now, session_factory=maker)
        finally:
            await engine.dispose()

    def _worker():
        loop = asyncio.new_event_loop()
        try:
            box["result"] = loop.run_until_complete(_go())
        except BaseException as e:            # noqa: BLE001 — пробрасываем в тест
            box["error"] = e
        finally:
            loop.close()

    t = threading.Thread(target=_worker)
    t.start()
    t.join(60)
    if "error" in box:
        raise box["error"]
    return box["result"]
