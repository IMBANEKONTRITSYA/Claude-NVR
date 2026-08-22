"""Резервное копирование на настоящем Postgres (SPEC §11, §18).

Здесь проверяется **production path** целиком: настоящий pg_dump снимает
дамп настоящей базы приложения, файл появляется в каталоге, отдаётся
скачиванием, удаляется — и всё это через HTTP, под ролями из §18.

Дамп из этих тестов не просто «непустой»: он проверяется на присутствие
таблиц приложения. Пустой или обрезанный дамп — самая опасная разновидность
зелёного теста в этом модуле: он выглядит как работающий бэкап ровно до
дня восстановления.
"""
import gzip
import shutil

import pytest

from app.services import backup


@pytest.fixture()
def bdir(tmp_path, monkeypatch):
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setattr(backup.settings, "BACKUP_PATH", str(directory))
    return directory


def test_status_endpoint_reports_empty_directory(client, admin_headers, bdir):
    r = client.get("/api/system/backups", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] == 0 and body["items"] == []
    assert body["dir"] == str(bdir)
    assert body["retention_days"] == backup.settings.BACKUP_RETENTION_DAYS
    # Готовность клиента показывается администратору заранее, а не
    # выясняется в день восстановления.
    assert "ready" in body["pg_dump"]


@pytest.mark.skipif(shutil.which("pg_dump") is None,
                    reason="pg_dump недоступен — production path не проверить")
def test_manual_backup_dumps_the_real_database(client, admin_headers, bdir):
    """§11 «ручной запуск»: кнопка в интерфейсе снимает дамп настоящей базы.

    До цикла 47 ручной запуск существовал единственным способом —
    `docker compose exec backup /backup/run.sh`, то есть требовал доступа к
    докер-сокету сервера, а в production (.deb + systemd, §26) бэкапа не
    было вовсе.
    """
    r = client.post("/api/system/backups", headers=admin_headers)
    assert r.status_code == 200, r.text
    info = r.json()
    path = bdir / info["name"]
    assert path.is_file() and info["size_bytes"] > 0

    dump = gzip.decompress(path.read_bytes()).decode("utf-8", "replace")
    # Не «файл непустой», а «в дампе есть, что восстанавливать»: схема,
    # таблицы приложения и расширение pgvector, без которого база не
    # поднимется вовсе.
    assert "CREATE TABLE" in dump
    for table in ("cameras", "users", "face_events", "video_segments"):
        assert f"public.{table}" in dump, f"в дампе нет таблицы {table}"
    assert "DROP TABLE IF EXISTS" in dump, "дамп снят без --clean --if-exists"

    listing = client.get("/api/system/backups", headers=admin_headers).json()
    assert [i["name"] for i in listing["items"]] == [info["name"]]
    assert listing["latest_age_hours"] is not None
    assert listing["latest_age_hours"] < 1


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump недоступен")
def test_backup_can_be_downloaded_and_deleted(client, admin_token, admin_headers, bdir):
    name = client.post("/api/system/backups", headers=admin_headers).json()["name"]

    # Скачивание идёт с токеном в query string (файл забирает сам браузер).
    r = client.get(f"/api/system/backups/{name}?token={admin_token}")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    assert gzip.decompress(r.content).decode("utf-8", "replace").count("CREATE TABLE") > 0

    assert client.delete(f"/api/system/backups/{name}", headers=admin_headers).status_code == 200
    assert not (bdir / name).exists()
    assert client.get("/api/system/backups", headers=admin_headers).json()["count"] == 0


def test_backup_routes_are_admin_only(client, make_user, bdir, request):
    """§18: строка «Управление бэкапами» — только админ.

    Проверяются все четыре роута и обе неадминские роли: дамп содержит всю
    базу целиком, включая хэши паролей и зашифрованные RTSP-учётки, поэтому
    «посмотреть список, но не трогать» здесь не отдельная ступень прав.

    Учётки заводятся фикстурой `make_user` — она удаляет их в финализаторе
    (см. её docstring: тест, оставивший пользователя, роняет не себя, а
    следующий прогон).
    """
    for role in ("operator", "viewer"):
        _, token = make_user(f"bk_{role}_{request.node.name}"[:60], role)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/system/backups", headers=headers).status_code == 403
        assert client.post("/api/system/backups", headers=headers).status_code == 403
        assert client.delete("/api/system/backups/facewatch_20260822_030000.sql.gz",
                             headers=headers).status_code == 403
        assert client.get(
            f"/api/system/backups/facewatch_20260822_030000.sql.gz?token={token}"
        ).status_code == 403


def test_backup_routes_reject_anonymous(client, bdir):
    assert client.get("/api/system/backups").status_code == 401
    assert client.post("/api/system/backups").status_code == 401
    # У скачивания токен — обязательный параметр запроса, поэтому его
    # отсутствие отсекается валидацией (422) раньше проверки прав, ровно
    # как у остальных ссылок с `?token=` (отчёты, экспорт аудита,
    # Prometheus). Важно здесь не число, а что файл не отдаётся.
    r = client.get("/api/system/backups/facewatch_20260822_030000.sql.gz")
    assert r.status_code in (401, 422)
    assert "gzip" not in r.headers.get("content-type", "")


@pytest.mark.parametrize("name", [
    "..%2F..%2Fetc%2Fpasswd",
    "%2Fetc%2Fpasswd",
    "not-a-backup.txt",
])
def test_download_rejects_paths_outside_the_directory(client, admin_token, bdir, name):
    r = client.get(f"/api/system/backups/{name}?token={admin_token}")
    assert r.status_code in (403, 404), r.text
    assert "root:" not in r.text


@pytest.mark.skipif(shutil.which("pg_dump") is None, reason="pg_dump недоступен")
def test_manual_backup_is_written_to_the_audit_log(client, admin_headers, bdir, pg_conn):
    """ТЗ §10/§13: журнал аудита фиксирует действия. Снятие копии — то
    действие, по которому потом восстанавливают картину «кто выгрузил базу
    объекта»."""
    name = client.post("/api/system/backups", headers=admin_headers).json()["name"]
    with pg_conn.cursor() as cur:
        cur.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 20")
        actions = [row[0] for row in cur.fetchall()]
    assert "Запущено резервное копирование" in actions
    client.delete(f"/api/system/backups/{name}", headers=admin_headers)
    with pg_conn.cursor() as cur:
        cur.execute("SELECT action FROM audit_log ORDER BY id DESC LIMIT 20")
        actions = [row[0] for row in cur.fetchall()]
    assert "Удалена резервная копия базы" in actions
