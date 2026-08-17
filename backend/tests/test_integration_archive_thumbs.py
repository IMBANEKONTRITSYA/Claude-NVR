"""Миниатюры кадров архива через настоящий эндпоинт (ТЗ §7).

`test_archive_thumbs.py` проверяет сервис; здесь — production path
целиком: настоящий Postgres, настоящий роутер, настоящая проверка роли по
БД и настоящий JPEG в теле ответа. Этот слой ловит то, чего не видно на
уровне сервиса, — матрицу прав §18, отбраковку путей за пределами
медиа-каталога и то, что битый сегмент даёт 404, а не 500 на всю выдачу.
"""
import os
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.services import thumbs

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="нужен ffmpeg")

SEG_DIR = os.path.join(settings.MEDIA_PATH, "segments")
BASE = datetime(2026, 4, 1, 9, 0, 0)


def _make_mp4(path: str, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=640x480:rate=10",
         "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", path],
        check=True,
    )


@pytest.fixture
def seeded_segments(pg_conn, request):
    """Камера и три сегмента: нормальный, битый и лежащий вне медиа-каталога.

    Возвращает `(cam_id, {"ok": id, "broken": id, "outside": id})`.
    """
    os.makedirs(SEG_DIR, exist_ok=True)
    outside_dir = "/tmp/fw-thumbs-outside"
    os.makedirs(outside_dir, exist_ok=True)

    ok_path = os.path.join(SEG_DIR, "thumb_ok.mp4")
    broken_path = os.path.join(SEG_DIR, "thumb_broken.mp4")
    outside_path = os.path.join(outside_dir, "thumb_outside.mp4")
    _make_mp4(ok_path, 5)
    _make_mp4(outside_path, 5)
    with open(broken_path, "wb") as fh:
        fh.write(b"this is not a video")

    ids = {}
    files = [ok_path, broken_path, outside_path]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('thumb-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        for key, path in (("ok", ok_path), ("broken", broken_path), ("outside", outside_path)):
            cur.execute(
                "INSERT INTO video_segments "
                "(camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes) "
                "VALUES (%s, %s, %s, %s, 'continuous', 5, %s) RETURNING id",
                (cam_id, BASE, BASE + timedelta(seconds=5), path, os.path.getsize(path)),
            )
            ids[key] = cur.fetchone()[0]

    def _cleanup():
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM video_segments WHERE id = ANY(%s)", (list(ids.values()),))
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))
        for p in files:
            try:
                os.remove(p)
            except OSError:
                pass
        for seg_id in ids.values():
            try:
                os.remove(thumbs.thumb_path(settings.MEDIA_PATH, seg_id))
            except OSError:
                pass

    request.addfinalizer(_cleanup)
    return cam_id, ids


def test_thumb_returns_jpeg(client, admin_token, seeded_segments):
    """Главная проверка §7: в выдаче есть кадр, а не только строка таблицы."""
    _, ids = seeded_segments
    r = client.get(f"/api/archive/thumb/{ids['ok']}", params={"token": admin_token})

    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content[:2] == b"\xff\xd8", "тело ответа не JPEG"
    assert len(r.content) > 500


def test_thumb_is_cached_on_disk_in_sharded_path(client, admin_token, seeded_segments):
    """Файл ложится туда, откуда его потом удалит ротация воркера.

    Путь собран здесь **литералом**, а не вызовом `thumbs.thumb_path()`:
    сверка функции с самой собой прошла бы при любой её правке, включая
    ту, что сломала бы раскладку. Поймано верификацией откатом — первая
    версия этой проверки была ложно-зелёной ровно так.

    Раскладку на стороне воркера сторожит `test_thumbs_parity.py`; вместе
    эти два теста и означают «бэкенд кладёт туда, откуда воркер удаляет».
    """
    _, ids = seeded_segments
    seg_id = ids["ok"]
    expected = os.path.join(settings.MEDIA_PATH, "thumbs",
                            str(seg_id // 1000), f"{seg_id}.jpg")
    if os.path.exists(expected):
        os.remove(expected)

    r = client.get(f"/api/archive/thumb/{seg_id}", params={"token": admin_token})
    assert r.status_code == 200

    assert os.path.exists(expected), f"миниатюра не легла в {expected}"
    assert os.path.getsize(expected) > 0


def test_thumb_second_request_does_not_run_ffmpeg(client, admin_token, seeded_segments):
    """Повторный запрос обслуживается кэшем.

    Выдача архива перезапрашивается на каждое уточнение фильтра; без кэша
    каждое стоило бы до 200 вызовов ffmpeg — на целевом сервере без GPU
    (§20) это отняло бы ядра у слоя аналитики.
    """
    _, ids = seeded_segments
    client.get(f"/api/archive/thumb/{ids['ok']}", params={"token": admin_token})

    calls = {"n": 0}
    original = thumbs.generate

    async def counting(src, dst, offset):
        calls["n"] += 1
        await original(src, dst, offset)

    thumbs.generate = counting
    try:
        r = client.get(f"/api/archive/thumb/{ids['ok']}", params={"token": admin_token})
    finally:
        thumbs.generate = original

    assert r.status_code == 200
    assert calls["n"] == 0


def test_thumb_sets_cache_header(client, admin_token, seeded_segments):
    """Закрытый сегмент иммутабелен — браузер не должен перекачивать кадр."""
    _, ids = seeded_segments
    r = client.get(f"/api/archive/thumb/{ids['ok']}", params={"token": admin_token})
    cache = r.headers.get("cache-control", "")
    assert "private" in cache and "max-age" in cache


def test_broken_segment_gives_404_not_500(client, admin_token, seeded_segments):
    """Битый сегмент — штатное состояние архива (обрыв RTSP на первой секунде).

    500 здесь означал бы, что одна повреждённая строка ломает страницу
    целиком; 404 оставляет выдачу читаемой, а строку — без картинки.
    """
    _, ids = seeded_segments
    r = client.get(f"/api/archive/thumb/{ids['broken']}", params={"token": admin_token})
    assert r.status_code == 404, r.text


def test_segment_outside_media_root_is_rejected(client, admin_token, seeded_segments):
    """Путь из БД уходит аргументом в ffmpeg — за пределы медиа он не ведёт.

    Та же проверка, что и у экспорта (`within_media_root`): строка,
    указывающая наружу, означает повреждение данных или запись, сделанную
    не слоем записи, и отдавать по ней кадр не нужно ни в одном из случаев.
    """
    _, ids = seeded_segments
    r = client.get(f"/api/archive/thumb/{ids['outside']}", params={"token": admin_token})
    assert r.status_code == 404, r.text
    assert not os.path.exists(thumbs.thumb_path(settings.MEDIA_PATH, ids["outside"]))


def test_unknown_segment_gives_404(client, admin_token):
    r = client.get("/api/archive/thumb/999999999", params={"token": admin_token})
    assert r.status_code == 404


def test_thumb_requires_token(client, seeded_segments):
    """Без токена и с чужим токеном кадр не отдаётся.

    Два разных кода — не небрежность, а два разных слоя: отсутствие
    обязательного query-параметра отбраковывает валидатор FastAPI (422,
    до обработчика), негодный токен — `get_user_from_query_token` (401).
    Проверяются оба, потому что важна не цифра, а то, что тела ответа с
    кадром нет ни в одном случае.
    """
    _, ids = seeded_segments
    url = f"/api/archive/thumb/{ids['ok']}"

    assert client.get(url).status_code == 422
    assert client.get(url, params={"token": "not-a-jwt"}).status_code == 401


def test_viewer_cannot_get_thumb(client, make_user, seeded_segments, request):
    """Матрица прав §18: архив наблюдателю закрыт.

    Кадр записи — это та же запись: если бы миниатюра была доступна
    наблюдателю, перебор id сегментов выдавал бы ему покадровый обзор
    всего архива в обход строки «Архив и экспорт».
    """
    _, ids = seeded_segments
    _, viewer_token = make_user(f"viewer-{request.node.name}"[:32], "viewer")
    r = client.get(f"/api/archive/thumb/{ids['ok']}", params={"token": viewer_token})
    assert r.status_code == 403, r.text


def test_thumbs_not_exposed_through_media_route(client, admin_token, seeded_segments):
    """`/api/media/thumbs/<файл>` не должен раздавать миниатюры.

    У `/api/media/{kind}/{name}` собственная таблица ролей
    (MEDIA_KIND_ROLES), и вид `thumbs` в неё сознательно не внесён: единственный
    вход — роутер архива, который сверяет роль с БД. Появись `thumbs` там
    наравне со `snapshots`, кадры архива стали бы доступны наблюдателю —
    ровно та дыра, которую закрывали для `segments`.
    """
    _, ids = seeded_segments
    client.get(f"/api/archive/thumb/{ids['ok']}", params={"token": admin_token})
    name = os.path.basename(thumbs.thumb_path(settings.MEDIA_PATH, ids["ok"]))

    r = client.get(f"/api/media/thumbs/{name}", params={"token": admin_token})
    assert r.status_code == 404, r.text
