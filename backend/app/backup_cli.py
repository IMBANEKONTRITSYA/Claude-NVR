"""Снятие дампа из командной строки: `python -m app.backup_cli`.

**Зачем отдельная точка входа, а если коротко — чтобы реализация была
одна.** §11 требует и автоматического бэкапа раз в сутки, и ручного
запуска. Ручной идёт кнопкой через `POST /api/system/backups`,
автоматический — systemd-таймером пакета (`facewatch-backup.timer`, §26).
Если бы таймер звал свой скрипт с собственным pg_dump и собственным
retention, две реализации разошлись бы на первой же правке — и разошлись
бы молча, потому что бэкап проверяют в день восстановления.

Здесь таймер зовёт ровно тот код, который стоит за кнопкой:
`services/backup.py`. Приложение при этом не поднимается — ни FastAPI, ни
пул соединений, ни воркеры: модуль тянет только конфигурацию.

Выход: 0 — дамп снят, 1 — отказ (сообщение уходит в stderr и, значит, в
journal — там его и увидит администратор, разбирая, почему копий нет).
"""
import json
import sys

from .services import backup


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--status" in argv:
        print(json.dumps(backup.status(), ensure_ascii=False, indent=2))
        return 0
    try:
        info = backup.run_backup()
    except backup.BackupError as exc:
        print(f"facewatch-backup: {exc}", file=sys.stderr)
        return 1
    print(f"facewatch-backup: снят дамп {info['name']} "
          f"({info['size_bytes']} байт за {info['elapsed_sec']} с)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
