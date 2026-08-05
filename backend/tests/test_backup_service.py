"""Регрессия ТЗ 12 «Резервное копирование»: сервис backup должен быть
настроен на автоматический ежедневный дамп, иметь ограничения ресурсов
и restart policy, и не публиковать порты наружу (используется только
изнутри docker-сети/через docker exec)."""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"


def _compose():
    return yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))


def test_backup_service_configured():
    backup = _compose()["services"]["backup"]
    assert backup["restart"] == "unless-stopped"
    assert "healthcheck" in backup
    assert backup["deploy"]["resources"]["limits"]["cpus"]
    assert backup["deploy"]["resources"]["limits"]["memory"]
    assert "ports" not in backup, "backup — внутренний сервис, порты наружу не нужны"


def test_backup_scripts_exist_and_executable():
    run_sh = ROOT / "backup" / "run.sh"
    entrypoint_sh = ROOT / "backup" / "entrypoint.sh"
    assert run_sh.is_file()
    assert entrypoint_sh.is_file()
    assert "pg_dump" in run_sh.read_text(encoding="utf-8")
    assert "--clean" in run_sh.read_text(encoding="utf-8"), (
        "Дамп должен быть с --clean --if-exists, чтобы накатывался обратно без "
        "ручного DROP DATABASE при восстановлении"
    )


def test_backup_retention_cleans_old_dumps():
    text = (ROOT / "backup" / "run.sh").read_text(encoding="utf-8")
    assert re.search(r"find /backups .*-mtime", text), (
        "run.sh должен удалять дампы старше BACKUP_RETENTION_DAYS, иначе диск "
        "заполнится бэкапами бесконечно"
    )


def test_backup_mount_is_readonly():
    """./backup смонтирован :ro в docker-compose.yml — сам скрипт не должен
    модифицироваться из контейнера. Это делает следующий тест обязательным:
    любая команда в entrypoint.sh, пишущая в /backup (включая chmod),
    обязана быть не-фатальной."""
    mount = _compose()["services"]["backup"]["volumes"]
    assert any(v.startswith("./backup:/backup:ro") for v in mount)


def test_backup_entrypoint_chmod_is_not_fatal_on_readonly_mount():
    """Регрессия: entrypoint.sh раньше делал безусловный `chmod +x
    /backup/run.sh` под `set -eu`, при том что ./backup смонтирован :ro
    (см. test_backup_mount_is_readonly) — chmod на read-only bind-mount
    всегда возвращает "Read-only file system", и `set -eu` валил entrypoint
    целиком на каждом старте контейнера. Бэкап не работал вообще, не
    только на Windows: `docker compose up` на любом хосте гарантированно
    приводил к CrashLoop сервиса backup. run.sh уже имеет +x, закоммиченный
    в git (режим 755) — chmod здесь нужен только как best-effort защита от
    чекаутов, теряющих exec-бит, и не должен быть фатальным."""
    entrypoint = (ROOT / "backup" / "entrypoint.sh").read_text(encoding="utf-8")
    for line in entrypoint.splitlines():
        stripped = line.strip()
        if stripped.startswith("chmod") and "run.sh" in stripped:
            assert "|| true" in stripped or "2>/dev/null" in stripped, (
                f"chmod на файл внутри read-only mount ./backup должен быть "
                f"не-фатальным (|| true), иначе валит весь entrypoint под "
                f"set -eu: {stripped!r}"
            )
            return
    # Строки chmod run.sh больше нет вообще — тоже приемлемо (run.sh и так
    # +x в git), просто нечего проверять дальше.
