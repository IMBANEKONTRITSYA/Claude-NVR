"""Удаление биометрии персоны по требованию (SPEC §24, 152-ФЗ).

§24 требует «возможность удаления данных по требованию». До этого модуля в
системе не было ни одного пути, который убирал бы биометрию человека:
`DELETE /api/persons/{pid}` сносил только карточку, а внешний ключ
`face_events.person_id` объявлен `ON DELETE SET NULL` — то есть все снимки
лица и все эмбеддинги 512D оставались в базе, просто без владельца.
Практическое следствие проверяется тестом: после «удаления» персоны
`POST /api/search/face` с её фотографией по-прежнему находит её кадры —
поиск идёт по `face_events.embedding` и на `person_id` не смотрит вовсе
(routers/search.py, ANN_SEARCH_SQL). Осиротевшие события при этом не
подбирает и рекластеризация воркера: она берёт только события, чей
`person_id` указывает на существующую персону со статусом `unknown`, —
данные остаются в базе, невидимые в интерфейсе и находимые поиском по фото.

**Порядок удаления обратный тому, что принят в уборке архива.**
`worker._drop_segments` сносит файл до строки, потому что файл без строки
не виден ни retention, ни циклической перезаписи и держит место навсегда.
Здесь наоборот — сначала строки, потом файлы:

* биометрический идентификатор — это эмбеддинг, а не картинка. Падение
  между двумя шагами не должно оставлять в базе вектор, по которому
  человека всё ещё найдёт поиск по фото;
* осиротевший JPEG самоисправляется: `snapshots/` и `avatars/` чистит
  `worker.prune_media` по возрасту файла, то есть остаток уходит в пределах
  срока хранения, а до того недостижим по API — на него не ссылается ни
  одна строка.
"""
import logging
import os

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import FaceEvent, Person

logger = logging.getLogger("facewatch.backend.biometrics")


def _is_inside(media_path: str, abs_path: str) -> bool:
    """Лежит ли `abs_path` строго внутри `media_path` (после разрешения ссылок).

    Пути в `snapshot_path`/`avatar_path` — относительные строки из базы, и
    удаление идёт по ним же. Значение вида `../../etc/passwd` в колонке
    (порча данных, восстановление чужого дампа, ошибка миграции) без этой
    проверки увело бы `os.remove` за пределы медиа-каталога.
    """
    root = os.path.realpath(media_path)
    target = os.path.realpath(abs_path)
    return target != root and os.path.commonpath([root, target]) == root


def _collect_paths(rows, avatar_path: str | None) -> list[str]:
    """Уникальные относительные пути файлов персоны, в стабильном порядке.

    `snapshot_path` и `orig_snapshot_path` события совпадают до апскейла
    (worker пишет в обе колонки один и тот же файл) и расходятся после;
    аватар персоны, заведённой автоматически, — это ссылка на снимок
    одного из её же событий (`worker`: `person.avatar_path = p.snap_rel`).
    Поэтому список именно уникальных путей, иначе второй `os.remove` того
    же файла ушёл бы в «не найден» и исказил счётчик в ответе.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for rel in (row.snapshot_path, row.orig_snapshot_path):
            if rel and rel not in seen:
                seen.add(rel)
                paths.append(rel)
    if avatar_path and avatar_path not in seen:
        seen.add(avatar_path)
        paths.append(avatar_path)
    return paths


async def _still_referenced(db: AsyncSession, paths: list[str]) -> set[str]:
    """Какие из `paths` ещё упомянуты выжившими строками.

    Проверяется после удаления строк персоны, то есть по выжившим. Случай
    редкий, но настоящий: слияние персон переносит события к цели, а аватар
    источника мог указывать на снимок одного из перенесённых событий. Без
    этой проверки удаление источника выбило бы картинку из-под чужой
    карточки — молча, потому что строка осталась бы на месте.

    Два запроса на весь список, а не два на файл. У персоны, прожившей на
    объекте месяц, кадров тысячи, и проверка по одному давала бы столько же
    пар round-trip'ов к Postgres — секунды на ровном месте в обработчике,
    который и так держит соединение открытым. `IN` по списку строк идёт по
    тем же индексам, что и сравнение по одной.
    """
    if not paths:
        return set()
    referenced: set[str] = set()
    for column in (FaceEvent.snapshot_path, FaceEvent.orig_snapshot_path):
        rows = await db.execute(select(column).where(column.in_(paths)).distinct())
        referenced.update(r[0] for r in rows if r[0])
    rows = await db.execute(
        select(Person.avatar_path).where(Person.avatar_path.in_(paths)).distinct()
    )
    referenced.update(r[0] for r in rows if r[0])
    return referenced


async def erase_person(db: AsyncSession, media_path: str, pid: int) -> dict | None:
    """Стирает персону вместе со всей её биометрией. `None` — персоны нет.

    Returns:
        Счётчики: сколько событий снято, сколько файлов удалено, сколько
        оставлено (файл отсутствовал на диске либо на него ссылается
        выжившая строка). Числа уходят в ответ API и в журнал аудита:
        «удалено» без количества не отличить от «нечего было удалять», а
        подтверждать исполнение требования об удалении приходится именно
        числом.
    """
    person = await db.get(Person, pid)
    if person is None:
        return None

    rows = (await db.execute(
        select(FaceEvent.snapshot_path, FaceEvent.orig_snapshot_path)
        .where(FaceEvent.person_id == pid)
    )).all()
    paths = _collect_paths(rows, person.avatar_path)

    # Шаг 1: строки. Одна транзакция — эмбеддинги персоны исчезают целиком
    # либо не исчезают вовсе; промежуточного состояния, в котором поиск по
    # фото находит половину кадров, не существует.
    res = await db.execute(delete(FaceEvent).where(FaceEvent.person_id == pid))
    events_removed = res.rowcount or 0
    await db.execute(delete(Person).where(Person.id == pid))
    await db.commit()

    # Шаг 2: файлы. Уже после коммита — см. модульный docstring про порядок.
    referenced = await _still_referenced(db, paths)
    removed = kept = 0
    for rel in paths:
        if rel in referenced:
            kept += 1
            continue
        abs_path = os.path.join(media_path, rel)
        if not _is_inside(media_path, abs_path):
            logger.warning("путь снимка вне медиа-каталога, файл не тронут",
                           extra={"person_id": pid, "rel_path": rel})
            kept += 1
            continue
        try:
            os.remove(abs_path)
            removed += 1
        except FileNotFoundError:
            kept += 1
        except OSError:
            logger.error("не удалось удалить файл биометрии",
                         extra={"person_id": pid, "rel_path": rel}, exc_info=True)
            kept += 1

    logger.info("удалена биометрия персоны по требованию (SPEC §24)",
                extra={"person_id": pid, "events_removed": events_removed,
                       "files_removed": removed, "files_kept": kept})
    return {"events_removed": events_removed,
            "files_removed": removed,
            "files_kept": kept}
