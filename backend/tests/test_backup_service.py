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


# --------------------------------------------------------------------------
# SPEC §11 «автоматическое раз в сутки» в PRODUCTION-режиме (§26, .deb+systemd).
#
# До цикла 47 контейнер `backup` был единственным местом, где вообще
# снимался дамп, — а живёт он только в docker-compose, который §26 прямо
# называет НЕ production. Пакет заводил каталог /var/lib/facewatch/backups
# (facewatch-first-run) и никто никогда ничего туда не писал: на боевом
# объекте «автоматическое раз в сутки» не выполнялось.
#
# Проверки ниже статические (systemd в песочнице нет), но ловят именно тот
# отказ: юнита нет, таймер не включается, служба не входит в конфигурацию.

SYSTEMD = ROOT / "packaging" / "deb" / "systemd"


def test_package_ships_a_daily_backup_timer():
    timer = (SYSTEMD / "facewatch-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=" in timer, "у таймера бэкапа нет расписания"
    # Persistent: сервер видеонаблюдения выключают редко, но выключают, и
    # пропущенный дамп должен сниматься после загрузки, а не через сутки.
    assert "Persistent=true" in timer
    assert "WantedBy=timers.target" in timer


def test_backup_service_runs_the_same_code_as_the_button():
    """Одна реализация на автоматический и ручной запуск (§11).

    Таймер, зовущий собственный скрипт со своим pg_dump и своим
    retention, разошёлся бы с кнопкой в интерфейсе на первой же правке —
    и разошёлся бы молча: бэкап проверяют в день восстановления.
    """
    unit = (SYSTEMD / "facewatch-backup.service").read_text(encoding="utf-8")
    assert "app.backup_cli" in unit, (
        "юнит должен звать тот же код, что и POST /api/system/backups "
        "(backend/app/backup_cli.py)"
    )
    assert "Type=oneshot" in unit
    assert "User=facewatch" in unit, "дамп не должен сниматься от root"
    assert "/etc/facewatch/facewatch.env" in unit, "юниту нужен DATABASE_URL"


def test_postinst_enables_the_backup_timer():
    """Таймер не входит в facewatch.target (иначе `restart facewatch.target`
    на апгрейде прерывал бы идущий дамп), поэтому `systemctl restart
    facewatch.target` его не поднимет — postinst обязан включить его сам."""
    postinst = (ROOT / "packaging" / "deb" / "postinst").read_text(encoding="utf-8")
    assert re.search(r"systemctl enable --now facewatch-backup\.timer", postinst), (
        "postinst не включает facewatch-backup.timer — «автоматическое раз "
        "в сутки» (§11) в production не работает"
    )


def test_prerm_disables_the_backup_timer():
    prerm = (ROOT / "packaging" / "deb" / "prerm").read_text(encoding="utf-8")
    assert "facewatch-backup.timer" in prerm, (
        "оставленный включённым таймер будил бы несуществующий юнит после "
        "удаления пакета"
    )


def test_build_script_installs_timer_units():
    build = (ROOT / "packaging" / "build-deb.sh").read_text(encoding="utf-8")
    assert re.search(r"systemd/\"\*\.timer", build) or "*.timer" in build, (
        "build-deb.sh кладёт в пакет только *.service — таймер бэкапа не "
        "попадёт в .deb"
    )


def test_package_env_template_points_backups_to_the_data_disk():
    template = (ROOT / "packaging" / "deb" / "conf"
                / "facewatch.env.template").read_text(encoding="utf-8")
    assert "BACKUP_PATH=/var/lib/facewatch/backups" in template, (
        "без BACKUP_PATH пакет писал бы дампы в /backups — каталог, которого "
        "на боевой машине нет"
    )


def test_first_run_creates_the_backup_directory():
    """Каталог должен существовать до первого срабатывания таймера: юнит
    работает под непривилегированным facewatch и /var/lib создать не сможет."""
    script = (ROOT / "packaging" / "deb" / "scripts"
              / "facewatch-first-run").read_text(encoding="utf-8")
    assert "backups" in script


def test_backend_image_ships_matching_pg_dump():
    """Версия pg_dump обязана быть НЕ СТАРШЕ сервера — иначе он
    отказывается работать вовсе, и узнают об этом в день восстановления.

    Сверяется с образом postgres из docker-compose.yml, а не с
    зафиксированным числом: разъехаться этим двум местам негде.
    """
    dockerfile = (ROOT / "backend" / "Dockerfile").read_text(encoding="utf-8")
    image = _compose()["services"]["postgres"]["image"]
    server_major = int(re.search(r"pg(\d+)", image).group(1))
    m = re.search(r"ARG PG_MAJOR=(\d+)", dockerfile)
    assert m, "в backend/Dockerfile нет PG_MAJOR — pg_dump в образе не ставится"
    assert int(m.group(1)) >= server_major, (
        f"pg_dump {m.group(1)} старше сервера {server_major}: ручной запуск "
        "бэкапа (§11) в docker-режиме не заработает"
    )
    assert "postgresql-client-${PG_MAJOR}" in dockerfile


def test_backend_can_reach_the_backup_directory_in_compose():
    """Кнопка «Создать бэкап» пишет в тот же том, что и cron-контейнер:
    иначе интерфейс показывал бы половину копий, а вторая была бы не видна."""
    backend = _compose()["services"]["backend"]
    assert any(v.startswith("backups:/backups") for v in backend["volumes"]), (
        "тому с бэкапами не видно из backend — список копий и ручной запуск "
        "работать не будут"
    )
    assert backend["environment"]["BACKUP_PATH"] == "/backups"
