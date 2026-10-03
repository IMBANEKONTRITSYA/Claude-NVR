"""Интеграционные тесты /api/users на реальном Postgres+Redis (см.
conftest.py:client, admin_headers). Покрывает защиту от self-lockout — до
этого фикса DELETE /api/users/{id} безусловно удалял любого пользователя,
включая самого вызывающего admin'а и последнего оставшегося администратора
вообще. После такого удаления управление пользователями/камерами/настройками
становится недоступно никому до перезапуска backend (main.py:lifespan
досеивает "admin" только если такого username вообще не существует, а не по
фактическому количеству админов)."""


def _create_user(make_user, request, role: str = "operator") -> dict:
    """Учётка заводится общей фикстурой (conftest.py:make_user), которая
    удалит её после теста. Повторное удаление тем же id в финализаторе
    безвредно: `DELETE /api/users/{id}` на несуществующем отвечает 404."""
    username = f"u_{request.node.name}"[:60]
    user_id, token = make_user(username, role)
    return {"id": user_id, "username": username, "token": token}


def test_admin_cannot_delete_own_account(client, admin_headers):
    """Единственный admin в системе (сид main.py:lifespan) не может удалить
    сам себя — иначе система остаётся без единого администратора."""
    me = client.get("/api/auth/me", headers=admin_headers).json()
    r = client.delete(f"/api/users/{me['id']}", headers=admin_headers)
    assert r.status_code == 400

    # Учётная запись реально не удалена
    assert client.get("/api/auth/me", headers=admin_headers).status_code == 200


def test_admin_can_delete_other_admin_while_not_last(client, admin_headers, make_user, request):
    """Проверка не должна ложно срабатывать: пока в системе двое админов,
    один может удалить другого (это не «последний администратор»)."""
    second_admin = _create_user(make_user, request, role="admin")
    r = client.delete(f"/api/users/{second_admin['id']}", headers=admin_headers)
    assert r.status_code == 200, r.text


def test_operator_cannot_delete_users(client, make_user, request):
    op = _create_user(make_user, request, role="operator")
    op_headers = {"Authorization": f"Bearer {op['token']}"}
    r = client.delete(f"/api/users/{op['id']}", headers=op_headers)
    assert r.status_code == 403


def test_admin_can_delete_other_non_admin_user(client, admin_headers, make_user, request):
    op = _create_user(make_user, request, role="operator")
    r = client.delete(f"/api/users/{op['id']}", headers=admin_headers)
    assert r.status_code == 200, r.text

    r = client.get("/api/users", headers=admin_headers)
    assert all(u["id"] != op["id"] for u in r.json())


def test_delete_nonexistent_user_returns_404(client, admin_headers):
    r = client.delete("/api/users/999999", headers=admin_headers)
    assert r.status_code == 404
