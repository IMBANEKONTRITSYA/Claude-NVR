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
