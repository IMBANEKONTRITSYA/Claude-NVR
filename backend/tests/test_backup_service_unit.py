"""Резервное копирование (SPEC §11) — чистая часть, без БД.

Сюда попадает то, что решает судьбу дампа задолго до pg_dump: какие имена
модуль признаёт своими, что считается устаревшей копией, и — главное —
что имя из HTTP-запроса не может увести чтение или удаление за пределы
каталога копий.
"""
import gzip
import os
import subprocess
from datetime import datetime, timezone

import pytest

from app.services import backup


@pytest.fixture
def bdir(tmp_path, monkeypatch):
    """Каталог копий — подкаталог tmp_path, а не сам tmp_path: рядом
    тесты кладут двойника pg_dump, и он попадал бы в проверки «в каталоге
    не осталось мусора»."""
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setattr(backup.settings, "BACKUP_PATH", str(directory))
    return directory


def _fake_pg_dump(tmp_path, body: str, name: str = "fake_pg_dump"):
    """Двойник pg_dump.

    Отвечает на `--version` обязательно: модуль спрашивает версию до
    запуска (см. `probe()` — клиент старше сервера не работает вовсе), и
    двойник без этого ответа проверял бы не тот путь кода, что боевой.
    """
    path = tmp_path / name
    path.write_text('#!/bin/sh\n'
                    'if [ "$1" = "--version" ]; then echo "pg_dump (PostgreSQL) 16.13"; exit 0; fi\n'
                    + body)
    path.chmod(0o755)
    return path


def _touch(directory, name: str, size: int = 10):
    path = directory / name
    path.write_bytes(b"x" * size)
    return path


def test_only_own_names_are_listed(bdir):
    """В каталог копий кладут и посторонние файлы (README, чужой дамп,
    временный .part). В списке их быть не должно: интерфейс обещает
    администратору, что каждая строка — восстановимая копия FaceWatch."""
    _touch(bdir, "facewatch_20260822_030000.sql.gz")
    _touch(bdir, "facewatch_20260821_030000.sql.gz")
    _touch(bdir, "facewatch_20260822_031500.sql.gz.part")
    _touch(bdir, "somebody_elses.sql.gz")
    _touch(bdir, "README.txt")
    names = [i["name"] for i in backup.list_backups()]
    assert names == ["facewatch_20260822_030000.sql.gz",
                     "facewatch_20260821_030000.sql.gz"]


def test_listing_is_newest_first(bdir):
    for name in ("facewatch_20260801_030000.sql.gz",
                 "facewatch_20260822_030000.sql.gz",
                 "facewatch_20260815_030000.sql.gz"):
        _touch(bdir, name)
    names = [i["name"] for i in backup.list_backups()]
    assert names == sorted(names, reverse=True)


@pytest.mark.parametrize("name", [
    "../../etc/passwd",
    "..%2f..%2fetc%2fpasswd",
    "/etc/passwd",
    "facewatch_20260822_030000.sql.gz/../../../etc/shadow",
    "facewatch_2026-08-22_030000.sql.gz",
    "",
])
def test_resolve_rejects_anything_but_own_name(bdir, name):
    """Имя сверяется с шаблоном целиком до склейки с каталогом, поэтому
    обход каталога невозможен по построению, а не по списку запрещённых
    подстрок. Тест держит это свойство: как только `resolve` начнёт
    склеивать раньше проверки, первый же случай пройдёт."""
    with pytest.raises(backup.BackupError):
        backup.resolve(name)


def test_resolve_rejects_missing_file(bdir):
    with pytest.raises(backup.BackupError):
        backup.resolve("facewatch_20260822_030000.sql.gz")


def test_delete_removes_only_named_copy(bdir):
    keep = _touch(bdir, "facewatch_20260821_030000.sql.gz")
    drop = _touch(bdir, "facewatch_20260822_030000.sql.gz")
    backup.delete(drop.name)
    assert not drop.exists()
    assert keep.exists()


def test_retention_uses_the_name_not_mtime(bdir, monkeypatch):
    """Возраст берётся из имени. Проверяется тем, что mtime у старого
    дампа свежий: так выглядит каталог, скопированный на новый диск, — и
    по mtime retention не удалил бы ничего никогда."""
    monkeypatch.setattr(backup.settings, "BACKUP_RETENTION_DAYS", 14)
    old = _touch(bdir, "facewatch_20260701_030000.sql.gz")
    fresh = _touch(bdir, "facewatch_20260822_030000.sql.gz")
    now = datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc).timestamp()
    os.utime(old, (now, now))
    removed = backup.apply_retention(now=now)
    assert removed == [old.name]
    assert fresh.exists() and not old.exists()


def test_retention_disabled_by_zero_days(bdir):
    old = _touch(bdir, "facewatch_20200101_030000.sql.gz")
    assert backup.apply_retention(days=0) == []
    assert old.exists()


def test_status_reports_age_of_the_latest_copy(bdir, monkeypatch):
    """Ключевое число карточки §11: не «копии есть», а «копия свежая».
    Четырнадцать дампов недельной давности выглядят благополучно ровно до
    дня восстановления."""
    _touch(bdir, "facewatch_20260701_030000.sql.gz")
    state = backup.status()
    assert state["count"] == 1
    assert state["latest_age_hours"] > 24 * 30
    assert state["latest"]["name"] == "facewatch_20260701_030000.sql.gz"


def test_status_without_any_copies(bdir):
    state = backup.status()
    assert state["count"] == 0 and state["latest"] is None
    assert state["latest_age_hours"] is None


def test_dsn_decodes_url_escaped_password(monkeypatch):
    """Пароль пакет генерирует случайным, и в URL он приезжает
    процентно-закодированным. Передать его в pg_dump как есть — это
    «неверный пароль» на боевой машине и нигде больше."""
    monkeypatch.setattr(
        backup.settings, "DATABASE_URL",
        "postgresql+asyncpg://face%40watch:p%40ss%2Fword@db.local:5433/facewatch")
    conn = backup.dsn()
    assert conn.user == "face@watch"
    assert conn.password == "p@ss/word"
    assert (conn.host, conn.port, conn.dbname) == ("db.local", 5433, "facewatch")


def test_probe_reports_missing_binary(monkeypatch, bdir):
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP",
                        "/nonexistent/pg_dump_definitely_not_here")
    state = backup.probe()
    assert state["ready"] is False and "pg_dump" in state["reason"]


def test_run_backup_refuses_without_pg_dump(monkeypatch, bdir):
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP",
                        "/nonexistent/pg_dump_definitely_not_here")
    with pytest.raises(backup.BackupError):
        backup.run_backup()
    assert list(bdir.iterdir()) == []


def test_failed_dump_leaves_no_file_behind(bdir, tmp_path, monkeypatch):
    """Дамп, оборвавшийся на середине, не должен оставаться в каталоге:
    иначе список показывает «копию», которую нельзя накатить, и именно её
    администратор увидит как самую свежую."""
    fake = _fake_pg_dump(tmp_path,
                         "echo 'pg_dump: error: connection failed' >&2\n"
                         "echo partial-output\nexit 1\n")
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP", str(fake))
    with pytest.raises(backup.BackupError, match="connection failed"):
        backup.run_backup()
    assert list(bdir.iterdir()) == [], "остался мусор от упавшего дампа"


def test_successful_dump_is_gzip_and_listed(bdir, tmp_path, monkeypatch):
    fake = _fake_pg_dump(tmp_path, "echo '-- fake dump'\n")
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP", str(fake))
    info = backup.run_backup()
    path = bdir / info["name"]
    assert gzip.decompress(path.read_bytes()).decode().strip() == "-- fake dump"
    assert [i["name"] for i in backup.list_backups()] == [info["name"]]


def test_empty_dump_is_rejected(bdir, tmp_path, monkeypatch):
    """pg_dump с кодом 0 и пустым выводом — не бэкап. Такой файл в списке
    был бы худшей из возможных копий: он выглядит свежим."""
    fake = _fake_pg_dump(tmp_path, "exit 0\n")
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP", str(fake))
    with pytest.raises(backup.BackupError):
        backup.run_backup()
    assert list(bdir.iterdir()) == []


def test_password_is_not_passed_on_the_command_line(bdir, tmp_path, monkeypatch):
    """`ps` виден любому пользователю машины. Пароль БД уходит только через
    PGPASSWORD в окружении дочернего процесса."""
    fake = _fake_pg_dump(tmp_path,
                         'echo "argv: $*"\necho "pw: $PGPASSWORD"\n')
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP", str(fake))
    monkeypatch.setattr(backup.settings, "DATABASE_URL",
                        "postgresql+asyncpg://facewatch:sup3r-s3cret@127.0.0.1:5432/facewatch")
    info = backup.run_backup()
    text = gzip.decompress((bdir / info["name"]).read_bytes()).decode()
    argv = next(line for line in text.splitlines() if line.startswith("argv: "))
    assert "sup3r-s3cret" not in argv
    assert "-U facewatch" in argv and "-d facewatch" in argv
    assert "pw: sup3r-s3cret" in text


def test_two_dumps_do_not_run_at_once(bdir, tmp_path, monkeypatch):
    """Второй запуск во время идущего дампа — отказ, а не второй pg_dump:
    два дампа одной базы это двойная нагрузка на диск ради двух почти
    одинаковых файлов."""
    monkeypatch.setattr(backup, "_running", backup.threading.Lock())
    backup._running.acquire()
    try:
        with pytest.raises(backup.BackupError, match="уже выполняется"):
            backup.run_backup()
    finally:
        backup._running.release()


def test_cli_status_does_not_start_a_dump(bdir, capsys):
    from app import backup_cli
    assert backup_cli.main(["--status"]) == 0
    assert "retention_days" in capsys.readouterr().out
    assert list(bdir.iterdir()) == []


def test_cli_reports_failure_with_nonzero_exit(bdir, monkeypatch, capsys):
    """Таймер systemd узнаёт об отказе только по коду выхода: молчаливый 0
    означал бы «бэкап работает» в journal и в `systemctl status`."""
    monkeypatch.setattr(backup.settings, "BACKUP_PG_DUMP", "/nonexistent/pg_dump")
    from app import backup_cli
    assert backup_cli.main([]) == 1
    assert "facewatch-backup" in capsys.readouterr().err


def test_real_pg_dump_refuses_older_client_against_newer_server():
    """Не проверка нашего кода, а фиксация предпосылки, на которой стоит
    выбор версии клиента в Dockerfile и в зависимостях пакета: pg_dump
    старше сервера не работает вовсе. Если предпосылка когда-нибудь
    перестанет быть верной, об этом лучше узнать здесь.
    """
    binary = backup._dump_binary()
    if binary is None:
        pytest.skip("pg_dump недоступен в этом окружении")
    version = backup._binary_version(binary)
    assert version and version[0] >= 13
    out = subprocess.run([binary, "--help"], capture_output=True, text=True).stdout
    assert "--clean" in out and "--if-exists" in out
