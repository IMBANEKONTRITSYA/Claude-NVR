"""Матрица прав SPEC.md на «ссылочных» эндпоинтах — тех, где токен идёт в
query string, потому что ссылку открывает браузер напрямую.

Находка цикла 18: /api/media/{kind}/{name} раздавал каталог `segments`
(файлы видеоархива) любой аутентифицированной роли, включая наблюдателя,
которому строка «Архив» матрицы прав запрещает доступ.

Находка цикла 19: роль на всех таких эндпоинтах бралась из claim'а токена,
а не из БД. Подделать claim нельзя (токен подписан), но он фиксирует
состояние на момент выдачи: удалённый пользователь и разжалованный из
admin/operator сохраняли доступ до истечения access-токена (30 минут по
умолчанию), тогда как на эндпоинтах с `require_role` теряли его сразу —
`get_current_user` ходит за пользователем и его ролью в БД.

Эти тесты идут полным production path: реальный HTTP-запрос через
TestClient, реальные пользователи в Postgres, реальная выдача токена через
/api/auth/login — не вызов функции-обработчика с синтетическим токеном.
Прошлая версия файла вызывала `media_file(...)` напрямую и потому в
принципе не могла заметить находку цикла 19: расхождение было ровно между
claim'ом в токене и строкой в БД, а строки в БД в том тесте не было.
"""
import pytest

from app.auth import create_token
from app.config import settings
from app.main import MEDIA_KIND_ROLES

pytestmark = pytest.mark.usefixtures("client")

# Пароль сложный: create_user валидирует его политикой (schemas.py).
_PW = "Passw0rd!media"


@pytest.fixture()
def media_files(tmp_path, monkeypatch):
    """Кладёт по одному файлу в каждый каталог медиа во временном MEDIA_PATH.

    Файлы нужны, чтобы отличить отказ по правам (403) от «файла нет» (404):
    без них разрешённая роль тоже получила бы 404 и тест ничего бы не
    доказывал.
    """
    monkeypatch.setattr(settings, "MEDIA_PATH", str(tmp_path))
    names = {}
    for kind in MEDIA_KIND_ROLES:
        d = tmp_path / kind
        d.mkdir()
        name = "cam1_1754380000.mp4" if kind == "segments" else "cam1_face.jpg"
        (d / name).write_bytes(b"stub")
        names[kind] = name
    return names


@pytest.fixture()
def make_user(client, admin_headers):
    """Заводит пользователя с нужной ролью и возвращает (id, access-токен).

    Пользователи создаются и удаляются через настоящий API, поэтому в БД
    оказывается ровно то, что там оказалось бы в проде.
    """
    created = []

    def _make(username: str, role: str):
        r = client.post(
            "/api/users",
            json={"username": username, "password": _PW, "role": role},
            headers=admin_headers,
        )
        assert r.status_code == 200, r.text
        user_id = r.json()["id"]
        created.append(user_id)
        lr = client.post("/api/auth/login", data={"username": username, "password": _PW})
        assert lr.status_code == 200, lr.text
        return user_id, lr.json()["access_token"]

    yield _make

    for user_id in created:
        client.delete(f"/api/users/{user_id}", headers=admin_headers)


def test_viewer_cannot_read_archive_segments(client, media_files, make_user):
    """Наблюдатель («Архив: Нет» в матрице прав) не должен получать сегменты
    архива даже при точном попадании в имя файла."""
    _, token = make_user("rbac_viewer", "viewer")
    r = client.get(f"/api/media/segments/{media_files['segments']}?token={token}")
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_archive_roles_can_read_segments(client, media_files, make_user, role):
    """Админ и оператор («Архив: Да») по-прежнему получают файл, а не 403 —
    фикс не должен ломать легитимный доступ к архиву."""
    _, token = make_user(f"rbac_seg_{role}", role)
    r = client.get(f"/api/media/segments/{media_files['segments']}?token={token}")
    assert r.status_code == 200, r.text
    assert r.content == b"stub"


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
@pytest.mark.parametrize("kind", ["snapshots", "avatars"])
def test_all_roles_can_read_faces(client, media_files, make_user, role, kind):
    """Кадры лиц и аватары доступны всем ролям осознанно: на них построены
    Стена и дашборд, разрешённые наблюдателю той же матрицей прав."""
    _, token = make_user(f"rbac_{kind}_{role}", role)
    r = client.get(f"/api/media/{kind}/{media_files[kind]}?token={token}")
    assert r.status_code == 200, r.text
    assert r.content == b"stub"


def test_unknown_kind_still_404(client, media_files, admin_token):
    """Каталог вне матрицы — 404 (никакого раскрытия того, какие каталоги
    существуют, через различие 403/404)."""
    r = client.get(f"/api/media/uploads/anything.jpg?token={admin_token}")
    assert r.status_code == 404


def test_invalid_token_rejected(client, media_files):
    """Проверка подписи не должна была потеряться при переходе на БД."""
    r = client.get(f"/api/media/snapshots/{media_files['snapshots']}?token=не-токен")
    assert r.status_code == 401


def test_role_comes_from_db_not_from_token_claim(client, media_files, make_user):
    """Ядро находки цикла 19: claim `role` в токене не должен решать ничего.

    Токен подписан настоящим ключом сервера и утверждает, что предъявитель —
    admin, но в БД этот пользователь заведён наблюдателем. Именно так
    выглядит разжалованный пользователь со старым, ещё не истёкшим
    access-токеном: claim в нём остался прежним. Доступ должен определяться
    строкой в БД, то есть закончиться 403 на сегментах архива.
    """
    make_user("rbac_demoted", "viewer")
    stale_token = create_token("rbac_demoted", "admin")
    r = client.get(f"/api/media/segments/{media_files['segments']}?token={stale_token}")
    assert r.status_code == 403


def test_deleted_user_loses_access_immediately(client, media_files, make_user, admin_headers):
    """Удалённый пользователь теряет доступ сразу, а не по истечении токена.

    До фикса подпись токена была единственной проверкой: учётная запись
    уволенного сотрудника удалялась, а его access-токен ещё до получаса
    продолжал скачивать кадры со всех камер.
    """
    user_id, token = make_user("rbac_fired", "operator")
    url = f"/api/media/snapshots/{media_files['snapshots']}?token={token}"
    assert client.get(url).status_code == 200, "легитимный доступ до удаления"

    assert client.delete(f"/api/users/{user_id}", headers=admin_headers).status_code == 200
    assert client.get(url).status_code == 401


def test_media_kinds_match_archive_router_roles():
    """Страховка от расхождения: роли для `segments` должны совпадать с
    теми, что требует роутер архива. Если когда-нибудь поменяют одно место,
    тест укажет на второе."""
    assert MEDIA_KIND_ROLES["segments"] == ("admin", "operator")
