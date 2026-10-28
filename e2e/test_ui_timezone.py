"""Время в интерфейсе — в поясе объекта, а не в цифрах UTC.

До настройки `display_timezone` интерфейс выводил время сервера (UTC без
суффикса) через `new Date(ts).toLocaleString()`, а такую строку браузер
читает как местную: журнал и архив показывали UTC под видом местного
времени, фильтр «с 15:00» искал с 15:00 UTC. На объекте в UTC+5 (развёртывание
2026-10-28) это пять часов расхождения с часами на стене поста.

Проверяется в настоящем браузере через боевой nginx, потому что весь
дефект — в разборе строки движком браузера; юнит-тест модуля timeZone.ts
показывает арифметику, но не то, что страница ей действительно пользуется.

Браузер стенда работает в UTC. Это существенно: если бы пояс браузера
совпадал с поясом объекта, старый код и новый показали бы одно и то же, и
проверка не отличила бы их.
"""
import uuid

import pytest

OBJECT_TZ = "Asia/Yekaterinburg"     # UTC+5, без перевода часов
EVENT_UTC = "2026-10-28 09:30:00"
SHOWN_IN_OBJECT_TZ = "28.10.2026, 14:30:00"
SHOWN_IN_MOSCOW = "28.10.2026, 12:30:00"


@pytest.fixture()
def probe_row(pg_conn):
    """Строка журнала аудита в известный момент UTC и пояс объекта UTC+5."""
    user = f"tz-probe-{uuid.uuid4().hex[:8]}"
    with pg_conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key = 'display_timezone'")
        row = cur.fetchone()
        before = row[0] if row else None
        cur.execute(
            "INSERT INTO settings (key, value) VALUES ('display_timezone', %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (OBJECT_TZ,))
        cur.execute(
            "INSERT INTO audit_log (ts, username, role, action, method, path, status_code) "
            "VALUES (%s, %s, 'admin', 'Проверка пояса', 'GET', '/probe', 200) RETURNING id",
            (EVENT_UTC, user))
        row_id = cur.fetchone()[0]
    try:
        yield user
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE id = %s", (row_id,))
            if before is None:
                cur.execute("DELETE FROM settings WHERE key = 'display_timezone'")
            else:
                cur.execute("UPDATE settings SET value = %s WHERE key = 'display_timezone'", (before,))


def _filter_by_user(page, user):
    page.fill("input >> nth=0", user)
    page.wait_for_timeout(800)


def _first_cell(page):
    return page.locator("table tbody tr").first.locator("td").first.inner_text().strip()


def test_browser_runs_in_utc(logged_in):
    """Предпосылка проверки: пояс браузера стенда не совпадает с поясом объекта."""
    tz = logged_in.page.evaluate("Intl.DateTimeFormat().resolvedOptions().timeZone")
    assert tz in ("UTC", "Etc/UTC"), f"браузер стенда в {tz} — проверка не различит старый и новый код"


def test_audit_shows_object_time(logged_in, probe_row):
    page = logged_in.page
    logged_in.visit("/audit")
    _filter_by_user(page, probe_row)
    assert _first_cell(page) == SHOWN_IN_OBJECT_TZ


def test_audit_filter_reads_object_time(logged_in, probe_row):
    """«С 14:00» по часам объекта включает событие 14:30, «с 15:00» — нет."""
    page = logged_in.page
    logged_in.visit("/audit")
    _filter_by_user(page, probe_row)
    date_from = page.locator("input[type=datetime-local]").first
    date_from.fill("2026-10-28T14:00")
    page.wait_for_timeout(800)
    assert _first_cell(page) == SHOWN_IN_OBJECT_TZ
    date_from.fill("2026-10-28T15:00")
    page.wait_for_timeout(800)
    assert "Записей нет" in page.locator("table tbody").inner_text()


def test_changing_zone_in_settings_changes_what_is_shown(logged_in, probe_row):
    """Пояс меняет администратор — и время на страницах следует за ним."""
    page = logged_in.page
    logged_in.visit("/settings")
    select = page.locator("select:has(option[value='Europe/Moscow'])")
    select.select_option("Europe/Moscow")
    page.get_by_role("button", name="Сохранить").click()
    page.wait_for_selector("text=Сохранено", timeout=10000)
    logged_in.visit("/audit")
    _filter_by_user(page, probe_row)
    assert _first_cell(page) == SHOWN_IN_MOSCOW
