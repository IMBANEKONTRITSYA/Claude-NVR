"""Личность и роль на «ссылочных» эндпоинтах берутся из БД, а не из токена.

Часть эндпоинтов открывается браузером по прямой ссылке (выгрузки отчётов и
аудита, файл сегмента архива, кадр камеры, метрики Prometheus, WebSocket'ы),
поэтому токен там приходит параметром `?token=`, а не заголовком
Authorization. Способ доставки токена — единственное, чем они отличаются от
остальных; проверки должны быть теми же.

Находка цикла 19: до фикса каждый такой обработчик звал `jwt.decode` сам и
сравнивал с ролями claim `role` из токена, ни разу не заглядывая в БД.
Подделать claim нельзя — токен подписан ключом сервера, — но claim
фиксирует состояние на момент выдачи:

* разжалованный из admin в viewer до истечения access-токена (30 минут по
  умолчанию) продолжал выгружать журнал аудита и отчёты целиком;
* удалённый пользователь продолжал скачивать сегменты архива и кадры со
  всех камер.

На эндпоинтах с `require_role` то же самое отсекалось сразу: там
`get_current_user` идёт в БД за пользователем и его ролью. Расхождение было
не в дизайне, а в том, что проверка написана руками в обход общей, — и
поэтому здесь проверяется вся группа эндпоинтов разом, а не один из них.

Тесты идут полным production path: настоящий HTTP-запрос через TestClient,
настоящие пользователи в Postgres, настоящая выдача токена через
/api/auth/login. Синтетическим здесь остаётся ровно одно — «протухший»
токен с ролью, которой у пользователя в БД уже нет (create_token), потому
что именно он и воспроизводит разжалование.
"""
import pytest
from starlette.websockets import WebSocketDisconnect

from app.auth import create_token

pytestmark = pytest.mark.usefixtures("client")


# (описание, метод построения URL по токену, ожидаемый код для роли ниже
# требуемой). 403 — «роль не подходит», 401 — «такого пользователя нет».
QUERY_TOKEN_ENDPOINTS = [
    ("отчёт по появлениям (CSV)", lambda t: f"/api/reports/appearances.csv?token={t}", 403),
    ("отчёт по появлениям (XLSX)", lambda t: f"/api/reports/appearances.xlsx?token={t}", 403),
    ("отчёт по персонам", lambda t: f"/api/reports/persons.csv?token={t}", 403),
    ("отчёт по камерам", lambda t: f"/api/reports/cameras.csv?token={t}", 403),
    ("экспорт аудита (CSV)", lambda t: f"/api/audit/export.csv?token={t}", 403),
    ("экспорт аудита (XLSX)", lambda t: f"/api/audit/export.xlsx?token={t}", 403),
    ("скачивание сегмента архива", lambda t: f"/api/archive/file/1?token={t}", 403),
    ("метрики Prometheus", lambda t: f"/api/system/prometheus?token={t}", 403),
]



@pytest.mark.parametrize("label,url_for,expected", QUERY_TOKEN_ENDPOINTS,
                         ids=[e[0] for e in QUERY_TOKEN_ENDPOINTS])
def test_stale_admin_claim_does_not_grant_access(client, make_user, label, url_for, expected):
    """Токен утверждает `role: admin`, но в БД пользователь — наблюдатель.

    Так выглядит разжалованный пользователь со старым, ещё не истёкшим
    access-токеном. Решать должна строка в БД.
    """
    make_user("qt_demoted", "viewer")
    stale_token = create_token("qt_demoted", "admin")
    assert client.get(url_for(stale_token)).status_code == expected


@pytest.mark.parametrize("label,url_for,expected", QUERY_TOKEN_ENDPOINTS,
                         ids=[e[0] for e in QUERY_TOKEN_ENDPOINTS])
def test_deleted_user_token_rejected(client, admin_headers, make_user, label, url_for, expected):
    """Удалённая учётная запись теряет доступ сразу, а не по истечении токена."""
    user_id, token = make_user("qt_fired", "admin")
    assert client.get(url_for(token)).status_code != 401, "до удаления доступ должен быть"

    assert client.delete(f"/api/users/{user_id}", headers=admin_headers).status_code == 200
    assert client.get(url_for(token)).status_code == 401


@pytest.mark.parametrize("label,url_for,expected", QUERY_TOKEN_ENDPOINTS,
                         ids=[e[0] for e in QUERY_TOKEN_ENDPOINTS])
def test_legitimate_admin_still_allowed(client, make_user, label, url_for, expected):
    """Фикс не должен ломать легитимный доступ.

    Проверяется именно production path целиком: 401/403 не должно быть ни на
    одном эндпоинте группы. Сегмент архива с id=1 в чистой БД отсутствует —
    для него 404 и есть признак того, что проверка прав пройдена и
    обработчик дошёл до поиска файла.
    """
    make_user("qt_admin", "admin")
    token = create_token("qt_admin", "admin")
    assert client.get(url_for(token)).status_code not in (401, 403)


def test_snapshot_requires_existing_user(client, make_user, admin_headers):
    """Кадр камеры роль не проверяет (живой просмотр разрешён всем ролям),
    но существование учётной записи — обязан: иначе токен удалённого
    сотрудника ещё до получаса показывал бы картинку со всех камер."""
    user_id, token = make_user("qt_snapshot", "viewer")
    # Файла кадра нет — 404 означает, что проверка токена пройдена.
    assert client.get(f"/api/cameras/1/snapshot?token={token}").status_code == 404

    assert client.delete(f"/api/users/{user_id}", headers=admin_headers).status_code == 200
    assert client.get(f"/api/cameras/1/snapshot?token={token}").status_code == 401


def test_websocket_rejects_deleted_user_at_handshake(client, make_user, admin_headers):
    """WS проверял токен при подключении только на подпись.

    Пере-проверка по БД была, но только в цикле пинга (~30 секунд), поэтому
    удалённый пользователь успевал открыть новый сокет и получать события
    до первого пинга. Рукопожатие должно отказывать сразу.
    """
    user_id, token = make_user("qt_ws", "viewer")
    with client.websocket_connect(f"/ws/faces?token={token}") as ws:
        assert ws is not None

    assert client.delete(f"/api/users/{user_id}", headers=admin_headers).status_code == 200
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/faces?token={token}"):
            pass
