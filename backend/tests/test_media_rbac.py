"""Регрессионный тест: /api/media/{kind}/{name} обязан соблюдать матрицу
прав SPEC.md, а не только проверять подпись токена.

Находка цикла 18: эндпоинт раздавал каталог `segments` (файлы видеоархива)
любой аутентифицированной роли, включая наблюдателя, которому строка
«Архив» матрицы прав запрещает доступ. Роутер архива ограничение соблюдал
(routers/archive.py требует admin/operator и для списка сегментов, и для
скачивания файла), поэтому /api/media/segments/<файл> работал именно как
обход RBAC — тем более что имена сегментов предсказуемы
(`cam{camera_id}_{unix_ts}.mp4`, см. worker.py).

Тесты не требуют БД/Redis: обработчик `media_file` зависит только от
декодирования JWT и наличия файла на диске.
"""
import os

import pytest
from fastapi import HTTPException

from app.auth import create_token
from app.config import settings
from app.main import MEDIA_KIND_ROLES, media_file


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def media_files(tmp_path, monkeypatch):
    """Кладёт по одному файлу в каждый каталог медиа во временном MEDIA_PATH.

    Файлы нужны, чтобы отличить отказ по правам (403) от «файла нет» (404):
    без них разрешённая роль тоже получила бы 404 и тест ничего бы не
    доказывал.
    """
    monkeypatch.setattr(settings, "MEDIA_PATH", str(tmp_path))
    names = {}
    for kind in MEDIA_KIND_ROLES:
        d = tmp_path / kind
        d.mkdir()
        name = "cam1_1754380000.mp4" if kind == "segments" else "cam1_face.jpg"
        (d / name).write_bytes(b"stub")
        names[kind] = name
    return names


@pytest.mark.anyio
async def test_viewer_cannot_read_archive_segments(media_files):
    """Наблюдатель («Архив: Нет» в матрице прав) не должен получать сегменты
    архива даже при точном попадании в имя файла."""
    token = create_token("viewer1", "viewer")
    with pytest.raises(HTTPException) as exc:
        await media_file("segments", media_files["segments"], token=token)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("role", ["admin", "operator"])
@pytest.mark.anyio
async def test_archive_roles_can_read_segments(media_files, role):
    """Админ и оператор («Архив: Да») по-прежнему получают файл, а не 403 —
    фикс не должен ломать легитимный доступ к архиву."""
    token = create_token(f"{role}1", role)
    resp = await media_file("segments", media_files["segments"], token=token)
    assert os.path.basename(resp.path) == media_files["segments"]


@pytest.mark.parametrize("role", ["admin", "operator", "viewer"])
@pytest.mark.parametrize("kind", ["snapshots", "avatars"])
@pytest.mark.anyio
async def test_all_roles_can_read_faces(media_files, role, kind):
    """Кадры лиц и аватары доступны всем ролям осознанно: на них построены
    Стена и дашборд, разрешённые наблюдателю той же матрицей прав."""
    token = create_token(f"{role}1", role)
    resp = await media_file(kind, media_files[kind], token=token)
    assert os.path.basename(resp.path) == media_files[kind]


@pytest.mark.anyio
async def test_unknown_kind_still_404(media_files):
    """Каталог вне матрицы — 404, как и до фикса (никакого раскрытия того,
    какие каталоги существуют, через различие 403/404)."""
    token = create_token("admin1", "admin")
    with pytest.raises(HTTPException) as exc:
        await media_file("uploads", "anything.jpg", token=token)
    assert exc.value.status_code == 404


@pytest.mark.anyio
async def test_invalid_token_rejected(media_files):
    """Проверка подписи не должна была потеряться при добавлении проверки роли."""
    with pytest.raises(HTTPException) as exc:
        await media_file("snapshots", media_files["snapshots"], token="не-токен")
    assert exc.value.status_code == 401


@pytest.mark.anyio
async def test_token_without_role_claim_rejected(media_files):
    """Токен без claim'а role не должен трактоваться как «роль подходит»."""
    from jose import jwt

    token = jwt.encode({"sub": "x"}, settings.SECRET_KEY, algorithm="HS256")
    with pytest.raises(HTTPException) as exc:
        await media_file("snapshots", media_files["snapshots"], token=token)
    assert exc.value.status_code == 403


def test_media_kinds_match_archive_router_roles():
    """Страховка от расхождения: роли для `segments` должны совпадать с
    теми, что требует роутер архива. Если когда-нибудь поменяют одно место,
    тест укажет на второе."""
    assert MEDIA_KIND_ROLES["segments"] == ("admin", "operator")
