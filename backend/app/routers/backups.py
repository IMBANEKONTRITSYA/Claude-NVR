"""Управление резервными копиями (SPEC §11, §18).

§18 отводит бэкапам отдельную строку матрицы прав («Управление бэкапами:
админ да, оператор нет, наблюдатель нет») — до цикла 47 охранять ей было
нечего: в системе не было ни одного эндпоинта про бэкапы, а «ручной
запуск» из §11 существовал только как `docker compose exec`. Здесь эта
строка получает предмет.

**Все четыре роута admin-only**, включая чтение списка: имя дампа выдаёт
момент последнего снятия, а сам файл — всю базу целиком, вместе с
хэшами паролей и зашифрованными RTSP-учётками. Оператору из §18 бэкапы
недоступны, и «посмотреть, но не трогать» здесь не отдельная ступень.

Работу делает `services/backup.py`; здесь только HTTP: коды, валидация
имени и перевод блокирующих вызовов в пул потоков — pg_dump на базе
объекта идёт минуты, и держать на нём цикл событий значило бы остановить
на это время live-просмотр всех операторов.
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from ..auth import require_role, require_role_query
from ..services import backup

router = APIRouter(prefix="/api/system/backups", tags=["system"])


@router.get("")
async def list_backups(_=Depends(require_role("admin"))):
    """Состояние раздела + список копий (SPEC §11)."""
    status = await run_in_threadpool(backup.status)
    items = await run_in_threadpool(backup.list_backups)
    return {**status, "items": items}


@router.post("")
async def create_backup(_=Depends(require_role("admin"))):
    """Ручной запуск из §11.

    409, а не 500, когда дамп уже идёт: это не отказ системы, а состояние
    — второй администратор (или таймер) уже нажал кнопку.
    """
    try:
        return await run_in_threadpool(backup.run_backup)
    except backup.BackupError as exc:
        message = str(exc)
        code = 409 if "уже выполняется" in message else 400
        raise HTTPException(status_code=code, detail=message)


@router.get("/{name}")
async def download_backup(name: str, _=Depends(require_role_query("admin"))):
    """Скачивание дампа.

    Токен в query string, а не в заголовке: файл забирает сам браузер по
    прямой ссылке, как отчёты и выгрузку конфигурации камер. Роль при этом
    сверяется с `User.role` из БД (см. `require_role_query`), а не с claim'ом
    токена.
    """
    try:
        path = await run_in_threadpool(backup.resolve, name)
    except backup.BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(path, media_type="application/gzip", filename=path.name)


@router.delete("/{name}")
async def delete_backup(name: str, _=Depends(require_role("admin"))):
    try:
        await run_in_threadpool(backup.delete, name)
    except backup.BackupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"deleted": name}
