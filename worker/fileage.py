"""Возраст файлов на диске в том же виде времени, в котором система хранит
все свои метки, — naive-UTC.

Колонки времени в БД объявлены `DateTime` без `timezone=True`, то есть
`TIMESTAMP WITHOUT TIME ZONE`, и приложение кладёт в них UTC без tzinfo
(`datetime.utcnow()` в воркере, `auth.py:_utcnow_naive()` в бэкенде). Порог
удаления по retention считается от того же `utcnow()`.

`datetime.fromtimestamp(mtime)` без `tz` возвращает **локальное** время
хоста, и сравнение такого значения с UTC-порогом уезжает ровно на смещение
часового пояса. Дефолтные контейнеры проекта идут в UTC, поэтому ошибка
дремлет, но она становится настоящей при первой же переменной `TZ` в
`.env`/compose (обычное дело: логи в местном времени) или при запуске
воркера прямо на хосте, а не в контейнере:

* пояс восточнее UTC (`Asia/Yekaterinburg`, +05:00 — часовой пояс автора
  проекта): файлы выглядят на 5 часов новее, чем есть. Уборка осиротевших
  `*_tmp.mp4` идёт с окном 1 час, то есть окно перестаёт наступать раньше
  чем через 6 часов;
* пояс западнее UTC (`America/New_York`, −05:00): файлы выглядят на 5 часов
  старше. Свежий `*_tmp.mp4`, в который **прямо сейчас пишет ffmpeg**,
  проходит проверку «старше часа» и удаляется из-под пишущего процесса, а
  только что загруженное для поиска фото — из-под запроса.

Модуль намеренно без зависимостей сверх stdlib: он идёт в CI-джоб воркера,
который не ставит cv2/insightface/onnxruntime (см. .github/workflows/ci.yml).
"""
import logging
import os
from datetime import datetime, timezone
from typing import Callable

logger = logging.getLogger("facewatch.worker.fileage")


def mtime_utc(path: str) -> datetime:
    """Время последнего изменения файла как naive-UTC.

    Именно тот вид, с которым сравнимы `datetime.utcnow()` и метки времени
    из БД, — независимо от часового пояса хоста.
    """
    return datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).replace(tzinfo=None)


def prune_older_than(
    directory: str,
    cutoff: datetime,
    *,
    select: Callable[[str], bool] | None = None,
) -> int:
    """Удаляет файлы каталога, изменённые раньше `cutoff` (naive-UTC).

    Args:
        directory: каталог; отсутствующий каталог — не ошибка (0 удалений).
        cutoff: порог в naive-UTC, как его считает вызывающий от `utcnow()`.
        select: фильтр по имени файла; без него берутся все файлы.

    Returns:
        Число удалённых файлов.

    Ошибка по отдельному файлу (гонка с пишущим процессом, права, файл уже
    исчез) не прерывает обход остальных, но логируется: раньше все три
    цикла уборки в `worker.py` глушили её через `except Exception: pass`,
    из-за чего каталог, который перестал чиститься (например, сменились
    права после восстановления из бэкапа), выглядел бы как «уборка
    работает» до самого исчерпания диска.
    """
    if not os.path.isdir(directory):
        return 0
    removed = 0
    for name in os.listdir(directory):
        if select is not None and not select(name):
            continue
        path = os.path.join(directory, name)
        try:
            if not os.path.isfile(path):
                continue
            if mtime_utc(path) < cutoff:
                os.remove(path)
                removed += 1
        except FileNotFoundError:
            continue  # уже удалён параллельно — нормальный исход, не ошибка
        except OSError:
            logger.warning("не удалось удалить файл при уборке",
                           extra={"path": path}, exc_info=True)
    return removed


def prune_media(media_path: str, cutoff: datetime, tmp_cutoff: datetime) -> dict[str, int]:
    """Уборка медиа-каталогов по возрасту файлов (ТЗ 19: «автоудаление по
    retention-политике»). Возвращает {каталог: сколько удалено}.

    Вынесено из `worker.cleanup_old()` отдельно от работы с БД, чтобы
    политика удаления файлов проверялась в CI-джобе воркера: он не ставит
    cv2/insightface/onnxruntime, поэтому `import worker` там невозможен, и
    вся эта логика раньше оставалась без тестов вовсе.

    Args:
        media_path: корень MEDIA_PATH.
        cutoff: порог retention в naive-UTC (`utcnow() - retention_days`).
        tmp_cutoff: порог для осиротевших временных сегментов — окно короткое
            (час), потому что это следы падения процесса, а не история.
    """
    return {
        # cam{id}_latest.jpg — текущий кадр Стены: перезаписывается на месте,
        # историей не является, retention его не касается.
        "snapshots": prune_older_than(
            os.path.join(media_path, "snapshots"), cutoff,
            select=lambda name: not name.endswith("_latest.jpg"),
        ),
        # Фото, загруженные для поиска по лицу (backend routers/search.py
        # сохраняет их «для возможного экспорта/аудита»), не удалял никто:
        # retention до этого каталога не доходил, и он рос без границы всё
        # время работы системы. При 3–8 МБ на файл (типичный размер, см.
        # client_max_body_size в nginx-locations.conf) это заполнение диска,
        # то есть остановка записи архива, а не просто мусор. Срок тот же,
        # что у снимков: это такие же биометрические данные.
        "uploads": prune_older_than(os.path.join(media_path, "uploads"), cutoff),
        # Осиротевшие временные сегменты (например, после падения процесса).
        "segments_tmp": prune_older_than(
            os.path.join(media_path, "segments"), tmp_cutoff,
            select=lambda name: name.endswith("_tmp.mp4"),
        ),
    }
