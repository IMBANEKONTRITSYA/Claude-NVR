"""Резервное копирование БД (SPEC §11, §18).

§11 требует от системы двух вещей сразу: «резервное копирование:
**автоматическое раз в сутки + ручной запуск**». §18 отводит под это
отдельную строку матрицы прав — «Управление бэкапами: админ да, оператор
нет, наблюдатель нет», — то есть предполагает, что управлять бэкапами
можно **из системы**, а не из шелла на сервере.

**Что было до цикла 47.** Дамп снимал `backup/run.sh` в отдельном
контейнере docker-compose по busybox-cron. Из этого следовали две дыры,
каждая по своему пункту ТЗ:

* **в production бэкапа не было вовсе.** §26 объявляет production-режимом
  .deb + systemd, а контейнер `backup` живёт только в docker-compose,
  который §26 прямо называет НЕ production. Пакет заводил каталог
  `/var/lib/facewatch/backups` (`facewatch-first-run`) — и никто никогда
  ничего туда не писал. «Автоматическое раз в сутки» на боевом объекте не
  выполнялось;
* **«ручной запуск» существовал только как `docker compose exec`** —
  то есть требовал доступа к докер-сокету машины. Строка матрицы прав
  §18 не охраняла ничего: в интерфейсе бэкапов не было.

**Решение — дамп умеет снимать сам бэкенд.** Здесь ровно та же работа,
что делал `run.sh` (pg_dump → gzip → каталог, retention по возрасту), но
на стороне приложения: она доступна и из веб-интерфейса (кнопка «Создать
бэкап»), и по расписанию (systemd-таймер `facewatch-backup.timer` в
пакете, cron-контейнер в docker-compose — см. `docs/BACKUP.md`).

**Почему pg_dump, а не выгрузка средствами приложения.** Дамп обязан
годиться для восстановления — то есть содержать схему, последовательности,
права и расширения (`vector` в первую очередь). Своя выгрузка таблиц
воспроизводила бы pg_dump с неизбежными пропусками, а восстановление
проверяют один раз в жизни — в тот день, когда данные уже потеряны.

**Версия pg_dump обязана быть не старше сервера.** pg_dump 15 против
сервера 16 отказывается работать вовсе («server version mismatch»), и
узнать об этом в день восстановления — то же самое, что не иметь бэкапа.
Поэтому `probe()` проверяет версии заранее и показывает результат в
интерфейсе, а не молчит до первого запуска.

**Пароль БД не попадает ни в аргументы, ни в журнал.** pg_dump получает
его через `PGPASSWORD` в окружении дочернего процесса; в командной строке
(видной в `ps` любому пользователю машины) остаются только хост, порт,
пользователь и имя базы.
"""
from __future__ import annotations

import gzip
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..config import settings

logger = logging.getLogger("facewatch.backend")

# Имя дампа. Единственный формат, который модуль создаёт и признаёт своим:
# всё, что не подходит под шаблон, не попадает ни в список, ни в скачивание,
# ни в удаление, ни в retention.
#
# Это же и защита от обхода каталога: имя из запроса не склеивается с
# путём, пока не сверено с шаблоном целиком (`fullmatch`), а в шаблоне нет
# ни точек-родителей, ни слэшей. Проверять `..` и абсолютные пути отдельно
# не нужно — их просто нет в множестве допустимых имён.
NAME_RE = re.compile(r"facewatch_\d{8}_\d{6}\.sql\.gz")

# Недописанный дамп лежит под другим именем и переименовывается в конечное
# одним атомарным `os.replace`. Иначе список бэкапов показывал бы
# растущий файл как готовый, а падение pg_dump на середине оставляло бы
# «бэкап», который нельзя накатить.
PART_SUFFIX = ".part"

# Сколько ждать pg_dump. Дамп базы объекта на сотни тысяч событий идёт
# минуты; час — это «процесс завис», а не «база большая».
DUMP_TIMEOUT_SEC = 3600

# Ручной запуск не должен позволять двум администраторам (или админу и
# таймеру) писать в один каталог одновременно: два pg_dump на одной базе
# — это двойная нагрузка на диск ради двух почти одинаковых файлов.
_running = threading.Lock()


class BackupError(RuntimeError):
    """Ожидаемый отказ: нет pg_dump, не та версия, некуда писать, дамп упал."""


@dataclass(frozen=True)
class Dsn:
    host: str
    port: int
    user: str
    password: str
    dbname: str


def dsn() -> Dsn:
    """Параметры подключения из `DATABASE_URL`.

    Схема приходит с драйвером (`postgresql+asyncpg://`), pg_dump про
    драйверы ничего не знает — берутся только host/port/user/password/db.
    Значения url-декодируются: пароль, сгенерированный при установке
    пакета, содержит символы, которые в URL закодированы (`%2F` и т. п.),
    и передать их в pg_dump как есть значило бы «неверный пароль» на
    боевой машине и нигде больше.
    """
    parts = urllib.parse.urlsplit(settings.DATABASE_URL)
    return Dsn(
        host=parts.hostname or "localhost",
        port=parts.port or 5432,
        user=urllib.parse.unquote(parts.username or "postgres"),
        password=urllib.parse.unquote(parts.password or ""),
        dbname=(parts.path or "/").lstrip("/") or "postgres",
    )


def backup_dir() -> Path:
    return Path(settings.BACKUP_PATH)


def _dump_binary() -> str | None:
    """Путь к pg_dump или None, если его нет.

    Настройка `BACKUP_PG_DUMP` может содержать как имя (ищется в PATH), так
    и абсолютный путь: на Debian бинарники живут в
    `/usr/lib/postgresql/<версия>/bin`, которого в PATH сервиса нет.
    """
    configured = settings.BACKUP_PG_DUMP
    if os.path.isabs(configured):
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which(configured)


def _binary_version(path: str) -> tuple[int, ...] | None:
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True,
                             timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"(\d+)(?:\.(\d+))?", out)
    if not m:
        return None
    return tuple(int(g) for g in m.groups() if g is not None)


def probe() -> dict:
    """Готовность к снятию дампа — то, что показывается администратору.

    Отдельная функция, а не проверка внутри запуска: администратор должен
    видеть «pg_dump 15 против сервера 16» в спокойный день, а не в день
    восстановления. Ошибка здесь не бросается — состояние «не готов» это
    штатный ответ для интерфейса.
    """
    binary = _dump_binary()
    if binary is None:
        return {"ready": False, "binary": None, "version": None,
                "reason": f"pg_dump не найден ({settings.BACKUP_PG_DUMP}). "
                          "В production он ставится вместе с postgresql-client "
                          "той же версии, что и сервер."}
    version = _binary_version(binary)
    if version is None:
        return {"ready": False, "binary": binary, "version": None,
                "reason": "pg_dump найден, но не отвечает на --version"}
    return {"ready": True, "binary": binary,
            "version": ".".join(str(p) for p in version), "reason": None}


def _iter_dumps() -> list[Path]:
    directory = backup_dir()
    try:
        entries = list(directory.iterdir())
    except OSError:
        return []
    return [p for p in entries if p.is_file() and NAME_RE.fullmatch(p.name)]


def _describe(path: Path) -> dict:
    st = path.stat()
    return {
        "name": path.name,
        "size_bytes": st.st_size,
        "created_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc)
                              .isoformat(timespec="seconds"),
    }


def list_backups() -> list[dict]:
    """Дампы в каталоге, свежие первыми."""
    out = []
    for path in _iter_dumps():
        try:
            out.append(_describe(path))
        except OSError:
            # Файл удалили между обходом каталога и stat — не повод ронять
            # весь список.
            continue
    out.sort(key=lambda d: d["name"], reverse=True)
    return out


def resolve(name: str) -> Path:
    """Путь дампа по имени из запроса — или отказ.

    Имя сверяется с шаблоном ЦЕЛИКОМ до всякой склейки с каталогом, потому
    и обход каталога здесь невозможен по построению, а не по списку
    запрещённых подстрок (см. комментарий у `NAME_RE`).
    """
    if not NAME_RE.fullmatch(name or ""):
        raise BackupError("Недопустимое имя резервной копии")
    path = backup_dir() / name
    if not path.is_file():
        raise BackupError("Резервная копия не найдена")
    return path


def delete(name: str) -> None:
    path = resolve(name)
    try:
        path.unlink()
    except OSError as exc:
        raise BackupError(f"Не удалось удалить копию: {exc}") from exc


def apply_retention(days: int | None = None, *, now: float | None = None) -> list[str]:
    """Удаляет дампы старше `days` суток. Возвращает удалённые имена.

    Возраст берётся из имени файла, а не из mtime: имя проставляет тот, кто
    дамп снял, а mtime меняется от копирования каталога на другой диск и от
    восстановления из архива — то есть ровно от тех действий, которые
    администратор совершает с бэкапами.
    """
    keep_days = settings.BACKUP_RETENTION_DAYS if days is None else days
    if keep_days <= 0:
        return []
    horizon = (now if now is not None else time.time()) - keep_days * 86400
    removed = []
    for path in _iter_dumps():
        stamp = _stamp_of(path.name)
        if stamp is None or stamp.timestamp() > horizon:
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            logger.warning("не удалось удалить старый дамп",
                           extra={"backup": path.name}, exc_info=True)
    if removed:
        logger.info("удалены дампы старше срока хранения",
                    extra={"event": "backup_retention", "removed": len(removed),
                           "retention_days": keep_days})
    return removed


def _pipe(src, dst, chunk: int = 1 << 20) -> int:
    """Перекладывает поток и возвращает объём — тот, что до сжатия."""
    total = 0
    while True:
        buf = src.read(chunk)
        if not buf:
            return total
        dst.write(buf)
        total += len(buf)


def _stderr_sink():
    """Временный файл под stderr дочернего pg_dump (см. место вызова)."""
    return tempfile.TemporaryFile()


def _stamp_of(name: str) -> datetime | None:
    try:
        return datetime.strptime(name[len("facewatch_"):-len(".sql.gz")],
                                 "%Y%m%d_%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _new_name(now: datetime | None = None) -> str:
    ts = (now or datetime.now(timezone.utc)).strftime("%Y%m%d_%H%M%S")
    return f"facewatch_{ts}.sql.gz"


def run_backup(*, now: datetime | None = None) -> dict:
    """Снимает дамп. Возвращает описание готового файла.

    Поток такой же, как у `backup/run.sh`: pg_dump в текстовом формате с
    `--clean --if-exists` (дамп накатывается обратно на непустую базу без
    ручного DROP DATABASE), сжатие gzip, атомарная публикация, retention.

    Сжатие делает Python, а не флаг pg_dump: `-Z` у текстового формата
    появился только в PostgreSQL 16, а пакет должен работать и там, где
    сервер старше. Конвейер через shell не используется вовсе — иначе
    статус выхода pg_dump терялся бы за статусом gzip, и оборванный дамп
    выглядел бы успешным.
    """
    ready = probe()
    if not ready["ready"]:
        raise BackupError(ready["reason"])

    directory = backup_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"Каталог резервных копий недоступен: {exc}") from exc

    if not _running.acquire(blocking=False):
        raise BackupError("Резервное копирование уже выполняется")
    try:
        conn = dsn()
        name = _new_name(now)
        final = directory / name
        # PID в имени недописанного файла: блокировка `_running` — внутри
        # процесса, а писать в каталог могут двое (кнопка в бэкенде и
        # systemd-таймер). Без PID оба взяли бы один и тот же `.part` и
        # перемешали бы дампы в один файл; с ним худшее, что бывает, —
        # два одинаковых дампа, из которых останется последний.
        partial = directory / f"{name}.{os.getpid()}{PART_SUFFIX}"
        env = dict(os.environ)
        if conn.password:
            env["PGPASSWORD"] = conn.password
        cmd = [ready["binary"], "-h", conn.host, "-p", str(conn.port),
               "-U", conn.user, "-d", conn.dbname, "--clean", "--if-exists",
               "--no-password"]
        started = time.monotonic()
        try:
            # stderr — во временный файл, а не в трубу. Труба, которую никто
            # не читает, пока идёт копирование stdout, наполняется и
            # блокирует pg_dump навсегда; на успешном дампе он молчит, и
            # такой взаимоблокировки не видно ровно до первого отказа.
            with _stderr_sink() as errfile:
                with open(partial, "wb") as raw:
                    with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=6,
                                       # mtime=0: содержимое дампа не должно
                                       # зависеть от момента запуска больше,
                                       # чем на метку в имени файла.
                                       mtime=0) as gz:
                        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                                stderr=errfile, env=env)
                        assert proc.stdout is not None
                        try:
                            raw_bytes = _pipe(proc.stdout, gz)
                        finally:
                            proc.stdout.close()
                        try:
                            code = proc.wait(timeout=DUMP_TIMEOUT_SEC)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                            raise BackupError(
                                f"pg_dump не завершился за {DUMP_TIMEOUT_SEC} с")
                errfile.seek(0)
                err = errfile.read()
            if code != 0:
                tail = (err.decode("utf-8", "replace").strip().splitlines()
                        or ["код выхода %d" % code])[-1]
                raise BackupError(f"pg_dump завершился с ошибкой: {tail}")
        except BackupError:
            partial.unlink(missing_ok=True)
            raise
        except (OSError, subprocess.SubprocessError) as exc:
            partial.unlink(missing_ok=True)
            raise BackupError(f"Не удалось снять дамп: {exc}") from exc

        # Считается объём ДО сжатия: gzip от пустого ввода — это два
        # десятка байт заголовка, то есть по размеру файла пустой дамп
        # неотличим от непустого, и такая проверка молча пропускала бы
        # именно тот случай, ради которого написана.
        if raw_bytes == 0:
            partial.unlink(missing_ok=True)
            raise BackupError("pg_dump вернул пустой дамп")
        os.replace(partial, final)
        elapsed = round(time.monotonic() - started, 1)
    finally:
        _running.release()

    info = _describe(final)
    info["elapsed_sec"] = elapsed
    logger.info("снят дамп базы", extra={"event": "backup_created",
                                         "backup": info["name"],
                                         "size_bytes": info["size_bytes"],
                                         "elapsed_sec": elapsed})
    apply_retention()
    return info


def status() -> dict:
    """Состояние раздела бэкапов для интерфейса и для §9 «алерты»."""
    items = list_backups()
    directory = backup_dir()
    try:
        usage = shutil.disk_usage(directory)
        free_bytes: int | None = usage.free
    except OSError:
        free_bytes = None
    latest = items[0] if items else None
    latest_stamp = _stamp_of(latest["name"]) if latest else None
    age_hours = None
    if latest_stamp is not None:
        age_hours = round(
            (datetime.now(timezone.utc) - latest_stamp).total_seconds() / 3600, 1)
    return {
        "dir": str(directory),
        "schedule": settings.BACKUP_SCHEDULE,
        "retention_days": settings.BACKUP_RETENTION_DAYS,
        "count": len(items),
        "total_bytes": sum(i["size_bytes"] for i in items),
        "free_bytes": free_bytes,
        "latest": latest,
        # Отдельным числом, а не «пусть интерфейс посчитает от created_at»:
        # именно на него смотрит администратор, чтобы понять, работает ли
        # автоматический бэкап (§11) — сутки прошли, а свежего дампа нет.
        "latest_age_hours": age_hours,
        "running": _running.locked(),
        "pg_dump": probe(),
    }
