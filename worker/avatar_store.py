"""Аватар персоны живёт столько же, сколько карточка (SPEC §15).

`persons.avatar_path` заполняется двумя путями, и до цикла 53 они вели в
разные каталоги с **разными сроками жизни**:

* вручную, загрузкой фото (`routers/persons.py`) — файл кладётся в
  `avatars/`, а этот каталог уборка по возрасту не трогает намеренно
  (`fileage.prune_media`, сторож `test_prune_media_keeps_avatars`);
* автоматически, первым снимком лица (`worker.process_faces`) — путь
  указывал прямо в `snapshots/`, а `snapshots/` **чистится по возрасту**
  вместе со всей историей событий.

Второй случай означал, что у всякой персоны с автоматическим аватаром
карточка теряет фото через `retention_days` и больше не восстанавливается
никогда: воркер назначает аватар только когда `avatar_path is None`, а он
не `None` — он указывает на удалённый файл. Интерфейс отдаёт `<img>` на
несуществующий путь, то есть §15 «Галерея фотографий для каждой персоны»
на объекте с настройками по умолчанию разваливается на тридцатые сутки.

Отсюда две вещи, которые делает модуль:

* **снимок копируется в `avatars/`**, а не берётся ссылкой. Копия, а не
  перенос: на тот же файл ссылается `face_events.snapshot_path`, и унести
  его из-под события значило бы сломать ленту распознавания ради аватара;
* **ссылка в `snapshots/` считается недействительной**, если файла уже
  нет, — и тогда аватар назначается заново. Так лечатся карточки,
  заведённые до этого цикла: сама по себе миграция их не восстановит,
  файла уже нет, но следующий же раз, когда человека увидят, вернёт ему
  фото.

Проверка существования делается **только** для путей в `snapshots/`:
у аватара в `avatars/` срока жизни нет, и `stat` на каждое событие
распознавания был бы платой ни за что.

Только stdlib: модуль идёт в лёгкую CI-джобу воркера, которая не ставит
cv2/insightface/onnxruntime.
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid

logger = logging.getLogger("facewatch.worker.avatar_store")

AVATAR_DIR = "avatars"
SNAPSHOT_DIR = "snapshots"


def needs_avatar(avatar_path: str | None, media_root: str) -> bool:
    """Нужно ли назначить персоне аватар.

    `None` — никогда не назначался. Путь в `snapshots/`, которого больше
    нет на диске, — назначался, но пережит уборкой; такой считается
    отсутствующим, иначе карточка останется с битой картинкой навсегда.
    """
    if not avatar_path:
        return True
    if not avatar_path.startswith(SNAPSHOT_DIR + "/"):
        return False
    return not os.path.exists(os.path.join(media_root, avatar_path))


def adopt_snapshot(media_root: str, snap_rel: str) -> str | None:
    """Копирует снимок лица в `avatars/` и отдаёт относительный путь копии.

    `None`, если копию сделать не удалось, — вызывающий тогда аватар не
    назначает вовсе: ссылка на `snapshots/` была бы ровно тем, от чего
    модуль избавляется, а битая ссылка хуже пустой карточки.
    """
    if not snap_rel:
        return None
    src = os.path.join(media_root, snap_rel)
    dst_dir = os.path.join(media_root, AVATAR_DIR)
    # Имя не переиспользуется: снимок и аватар живут разное время, и
    # совпадение имён связало бы их удаление обратно. Префикс `auto_`
    # отличает от ручной загрузки (`manual_<hex>.jpg`) при разборе на
    # объекте.
    name = f"auto_{uuid.uuid4().hex}.jpg"
    try:
        os.makedirs(dst_dir, exist_ok=True)
        shutil.copyfile(src, os.path.join(dst_dir, name))
    except OSError:
        logger.warning("не удалось сохранить аватар персоны",
                       extra={"snapshot": snap_rel}, exc_info=True)
        return None
    return f"{AVATAR_DIR}/{name}"


def avatar_files(media_root: str) -> dict[str, str]:
    """Аватары на диске: {относительный путь: абсолютный}.

    Ключ — ровно та строка, что лежала бы в `persons.avatar_path`
    (`avatars/<имя>`), чтобы сверка с БД шла сравнением значений, а не
    разбором путей на двух сторонах.

    Каталог маленький: один файл на карточку с фотографией, а не на
    событие, — поэтому обход целиком дёшев и делается вместе с остальной
    часовой уборкой.
    """
    out: dict[str, str] = {}
    root = os.path.join(media_root, AVATAR_DIR)
    try:
        names = os.listdir(root)
    except OSError:
        return {}
    for name in names:
        path = os.path.join(root, name)
        if os.path.isfile(path):
            out[f"{AVATAR_DIR}/{name}"] = path
    return out
