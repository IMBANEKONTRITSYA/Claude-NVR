"""Роль в журнале аудита берётся из БД, а не из claim'а access-токена.

Последний открытый пункт класса, найденного циклом 18 и закрытого циклом 19
(PR #53) для *авторизации*: везде, где решение принималось по
`payload["role"]`, оно переведено на БД. Журнал аудита остался в стороне —
`audit.py:_extract_user()` возвращал `payload.get("role")`, и запись в
`audit_log` получала роль, замороженную на момент выдачи токена.

Access-токен живёт 30 минут (`ACCESS_TOKEN_EXPIRE_MINUTES`), поэтому окно
расхождения — до получаса после разжалования или удаления учётки.

Последствие не в доступе (он закрыт с цикла 19), а в достоверности
журнала. ТЗ 13 требует аудит как контроль; контроль, приписывающий
действие не той роли, хуже отсутствующего — он выглядит достоверным, и
разбор инцидента по нему приходит к неверному выводу о том, кто и с
какими правами действовал.

**Тесты идут production path целиком**: настоящий ASGI-стек с middleware
аудита (запись делает именно middleware, а не обработчик), настоящий
Postgres, настоящий выпущенный при логине JWT. Роль меняется в БД так же,
как её меняет администратор, а токен остаётся прежним — ровно то
состояние, в котором claim и БД расходятся.
"""
import pytest


def _latest_audit_row(pg_conn, username: str, action: str):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT role, action, status_code FROM audit_log "
            "WHERE username = %s AND action = %s ORDER BY id DESC LIMIT 1",
            (username, action),
        )
        return cur.fetchone()


@pytest.fixture()
def _cleanup_audit(pg_conn):
    """Записи журнала, оставленные тестом, убираются за ним (изоляция,
    цикл 21). Имена пользователей уникальны на тест, так что снимаются
    ровно свои строки."""
    names = []
    yield names
    if names:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE username = ANY(%s)", (names,))


def test_demoted_user_action_is_logged_with_current_role_not_token_claim(
    client, make_user, pg_conn, request, _cleanup_audit
):
    """Админ разжалован в operator; его старый токен пишет в журнал operator.

    До фикса запись получала `role='admin'` — роль из claim'а, которой у
    пользователя на момент действия уже не было.
    """
    username = f"audit_demoted_{request.node.name}"[:60]
    _cleanup_audit.append(username)
    user_id, token = make_user(username, "admin")
    headers = {"Authorization": f"Bearer {token}"}

    # Разжалование прямо в БД: тот же эффект, что у смены роли админом,
    # но токен на руках остаётся прежним — ради чего тест и написан.
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE users SET role = 'operator' WHERE id = %s", (user_id,))

    # Действие, которое operator'у разрешено и которое аудируется.
    r = client.get("/api/reports/appearances.csv", params={"token": token})
    assert r.status_code == 200, r.text

    row = _latest_audit_row(pg_conn, username, "Экспорт отчёта: появления (CSV)")
    assert row is not None, "действие не попало в журнал аудита"
    assert row[0] == "operator", (
        f"в журнал записана роль {row[0]!r} — она взята из claim'а токена, "
        "выпущенного до разжалования. Журнал приписывает действие роли, "
        "которой у пользователя уже нет"
    )


def test_deleted_user_action_is_logged_as_deleted_not_stale_role(
    client, admin_headers, make_user, pg_conn, request, _cleanup_audit
):
    """Учётка удалена, токен ещё жив — журнал не должен утверждать роль.

    Запрос отсекается авторизацией (401, закрыто циклом 19), но попытка
    всё равно попадает в журнал — и раньше с ролью `admin` из claim'а,
    как будто действовал администратор.
    """
    from app.audit import DELETED_USER_ROLE

    username = f"audit_deleted_{request.node.name}"[:60]
    _cleanup_audit.append(username)
    user_id, token = make_user(username, "admin")

    r = client.delete(f"/api/users/{user_id}", headers=admin_headers)
    assert r.status_code == 200, r.text

    r = client.get("/api/reports/appearances.csv", params={"token": token})
    assert r.status_code == 401, "удалённый пользователь не должен получать отчёт"

    row = _latest_audit_row(pg_conn, username, "Экспорт отчёта: появления (CSV)")
    assert row is not None, "попытка удалённого пользователя не попала в журнал"
    assert row[0] == DELETED_USER_ROLE, (
        f"в журнал записана роль {row[0]!r}; учётной записи в системе уже нет, "
        "и журнал не должен утверждать её роль по claim'у истекающего токена"
    )
    assert row[2] == 401


def test_normal_action_still_records_the_real_role(client, make_user, pg_conn, request, _cleanup_audit):
    """Контроль на пережатие: обычное действие пишет обычную роль.

    Без этой половины «исправлением» могло бы оказаться проставление
    заглушки всем подряд, и оба теста выше всё равно бы прошли.
    """
    username = f"audit_normal_{request.node.name}"[:60]
    _cleanup_audit.append(username)
    _, token = make_user(username, "operator")

    r = client.get("/api/reports/persons.csv", params={"token": token})
    assert r.status_code == 200, r.text

    row = _latest_audit_row(pg_conn, username, "Экспорт отчёта: персоны (CSV)")
    assert row is not None
    assert row[0] == "operator", f"ожидалась роль operator, записана {row[0]!r}"


def test_login_still_logged_with_placeholder_role(client, pg_conn):
    """Вход остаётся исключением: роль на момент записи ещё неизвестна.

    На /login токена нет по определению, а на неудачной попытке субъекта
    может не существовать вовсе — заглушка `?` здесь корректнее любого
    похода в БД, и фикс не должен был её тронуть.
    """
    from app.config import settings

    r = client.post("/api/auth/login", data={"username": "admin", "password": settings.ADMIN_PASSWORD})
    assert r.status_code == 200

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT role FROM audit_log WHERE username = 'admin' AND action = 'Вход в систему' "
            "ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
    assert row is not None, "вход не попал в журнал"
    assert row[0] == "?", f"роль на входе должна оставаться заглушкой, записана {row[0]!r}"
