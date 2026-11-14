"""Журнал аудита обязан назвать учётку входа при ЛЮБОМ транспорте формы.

Найдено циклом 63 живым опросом интерфейса настоящим браузером (Chromium)
через настоящий nginx перед настоящим бэкендом: в журнале аудита каждый
вход через веб-интерфейс стоял как `? (?)` — без имени пользователя.

Причина: `AuditMiddleware` разбирал тело `/api/auth/login` вызовом
`parse_qs`, который понимает ровно один формат —
`application/x-www-form-urlencoded`. Браузер шлёт форму входа как
`FormData` (`frontend/src/api.ts`: `login`), то есть
`multipart/form-data`; на нём `parse_qs` возвращал пустой словарь, и
middleware писала заглушку `"?"`.

Последствие — не в доступе, а в самом контроле из §10 («Журнал аудита всех
действий пользователей») и §14 («Аудит всех действий пользователей»):
единственный вопрос, ради которого журнал входов существует, — «кто и
когда вошёл» — оставался без ответа для **всех настоящих входов на
объекте**. Неудачные попытки задеты тем же: при переборе пароля из журнала
не видно даже, какую учётку перебирают.

**Почему это не поймали 62 цикла.** Все до единого теста, зовущие
`/api/auth/login`, передают форму как `data=` — то есть ровно тот
транспорт, который работал. Это carryover 21 цикла 62 дословно: «тест,
написанный под текущее поведение эндпоинта, не проверяет контракт
эндпоинта». Поэтому здесь проверяются **оба** транспорта: multipart —
потому что так ходит браузер, urlencoded — чтобы починка одного не увела
второй.
"""
import pytest


def _latest_login_row(pg_conn, username: str):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT username, status_code FROM audit_log "
            "WHERE username = %s AND action = 'Вход в систему' "
            "ORDER BY id DESC LIMIT 1",
            (username,),
        )
        return cur.fetchone()


def _login_rows_since(pg_conn, marker_id: int):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT username, status_code FROM audit_log "
            "WHERE id > %s AND action = 'Вход в систему' ORDER BY id",
            (marker_id,),
        )
        return cur.fetchall()


def _max_audit_id(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT COALESCE(MAX(id), 0) FROM audit_log")
        return cur.fetchone()[0]


@pytest.fixture()
def _cleanup_audit(pg_conn):
    names = []
    yield names
    if names:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE username = ANY(%s)", (names,))


def test_browser_multipart_login_names_the_account(client, pg_conn, _cleanup_audit):
    """Успешный вход тем же телом, что шлёт браузер (multipart/form-data).

    До фикса запись получала `username='?'`.
    """
    from app.config import settings

    marker = _max_audit_id(pg_conn)
    _cleanup_audit.append("admin")
    # files= вместо data= — httpx собирает multipart/form-data, ровно как
    # FormData в браузере. Значения без файла идут обычными полями формы.
    r = client.post(
        "/api/auth/login",
        files={"username": (None, "admin"), "password": (None, settings.ADMIN_PASSWORD)},
    )
    assert r.status_code == 200, r.text

    rows = _login_rows_since(pg_conn, marker)
    assert rows, "вход не попал в журнал вовсе"
    assert rows[-1][0] == "admin", (
        f"журнал не назвал учётку входа: записано {rows[-1][0]!r}. "
        "Вход через веб-интерфейс идёт multipart-формой (§10, §14)."
    )


def test_failed_multipart_login_names_the_attacked_account(client, pg_conn, _cleanup_audit):
    """Неудачная попытка обязана назвать перебираемую учётку.

    Именно эти записи разбирают после инцидента: без имени в журнале не
    видно, какую учётку подбирали, — а это ровно то, ради чего §14 требует
    аудит рядом с защитой от brute-force.
    """
    name = "audit_transport_victim"
    _cleanup_audit.append(name)
    marker = _max_audit_id(pg_conn)

    r = client.post(
        "/api/auth/login",
        files={"username": (None, name), "password": (None, "definitely-wrong-password")},
    )
    assert r.status_code == 401, r.text

    rows = _login_rows_since(pg_conn, marker)
    assert rows, "неудачная попытка входа не попала в журнал вовсе"
    assert rows[-1] == (name, 401), (
        f"неудачная попытка записана как {rows[-1]!r}, ожидалось {(name, 401)!r}"
    )


def test_urlencoded_login_still_names_the_account(client, pg_conn, _cleanup_audit):
    """Второй транспорт формы — тот, что работал до фикса.

    Стоит здесь не для симметрии: переход с `parse_qs` на штатный парсер
    Starlette меняет разбор обоих тел сразу, и починка multipart не должна
    была стоить urlencoded (им ходят curl, внешние интеграции §12 и все
    остальные тесты сьюта).
    """
    from app.config import settings

    marker = _max_audit_id(pg_conn)
    _cleanup_audit.append("admin")
    r = client.post(
        "/api/auth/login",
        data={"username": "admin", "password": settings.ADMIN_PASSWORD},
    )
    assert r.status_code == 200, r.text

    rows = _login_rows_since(pg_conn, marker)
    assert rows and rows[-1][0] == "admin", (
        f"urlencoded-вход перестал называть учётку: {rows!r}"
    )
