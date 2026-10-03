"""Экспорт фрагмента архива через настоящий эндпоинт (ТЗ §5).

`test_archive_export.py` проверяет сборку фрагмента; здесь — production
path целиком: настоящий Postgres, настоящий роутер, настоящая проверка
роли по БД и настоящий MP4 в теле ответа. Именно этот слой ловит то, чего
не видно на уровне сервиса: отбор сегментов по пересечению с окном,
границы параметров, матрицу прав §25 и запись в журнал аудита (§13).
"""
import os
import shutil
import subprocess
from datetime import datetime, timedelta

import pytest

from app.config import settings

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="нужны ffmpeg и ffprobe",
)

# Сегменты кладутся внутрь MEDIA_PATH: роутер отбраковывает файлы за его
# пределами (services/export.within_media_root), и тест обязан проверять
# работающий путь, а не обходить проверку.
SEG_DIR = os.path.join(settings.MEDIA_PATH, "segments")
BASE = datetime(2026, 3, 1, 10, 0, 0)


@pytest.fixture(scope="module", autouse=True)
def _drop_export_audit_rows():
    """Снять записи журнала, оставленные экспортами этого файла.

    Каждый успешный экспорт пишет строку в `audit_log` (ТЗ §13), и между
    прогонами по одной БД они накапливаются. Само по себе это безобидно, но
    ровно на этих накопленных строках проверка аудита ниже один раз уже
    оказалась ложно-зелёной: она находила строку предыдущего прогона и
    проходила при полностью выключенной записи в журнал.
    """
    yield
    import psycopg2
    from urllib.parse import urlsplit

    parsed = urlsplit(settings.DATABASE_URL.replace("+asyncpg", ""))
    try:
        conn = psycopg2.connect(
            host=parsed.hostname, port=parsed.port or 5432,
            user=parsed.username, password=parsed.password,
            dbname=parsed.path.lstrip("/"), connect_timeout=5,
        )
    except Exception:
        return
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE path = '/api/archive/export'")
    finally:
        conn.close()


def _make_mp4(path, seconds: int) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
         "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-c:v", "libx264", "-g", "10", "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )


@pytest.fixture
def seeded_archive(pg_conn, request):
    """Камера и три подряд идущих сегмента по 10 секунд на диске и в БД."""
    os.makedirs(SEG_DIR, exist_ok=True)
    files, seg_ids = [], []
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('export-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        for i in range(3):
            path = os.path.join(SEG_DIR, f"export_cam{cam_id}_{i}.mp4")
            _make_mp4(path, 10)
            files.append(path)
            cur.execute(
                "INSERT INTO video_segments "
                "(camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes) "
                "VALUES (%s, %s, %s, %s, 'continuous', 10, %s) RETURNING id",
                (cam_id, BASE + timedelta(seconds=10 * i),
                 BASE + timedelta(seconds=10 * (i + 1)), path, os.path.getsize(path)),
            )
            seg_ids.append(cur.fetchone()[0])

    def _cleanup():
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM video_segments WHERE id = ANY(%s)", (seg_ids,))
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))
        for p in files:
            try:
                os.remove(p)
            except OSError:
                pass

    request.addfinalizer(_cleanup)
    return cam_id


def _probe_duration(blob: bytes, tmp_path) -> float:
    out_file = tmp_path / "downloaded.mp4"
    out_file.write_bytes(blob)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(out_file)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def _params(cam_id, token, start_s, end_s):
    return {
        "camera_id": cam_id,
        "date_from": (BASE + timedelta(seconds=start_s)).isoformat(),
        "date_to": (BASE + timedelta(seconds=end_s)).isoformat(),
        "token": token,
    }


def test_export_returns_playable_fragment_across_segments(
    client, admin_token, seeded_archive, tmp_path
):
    """Окно поверх трёх сегментов приходит одним воспроизводимым MP4.

    Главная проверка §5: до этой правки оператору пришлось бы скачать три
    файла целиком (30 секунд) и склеивать их вручную.
    """
    r = client.get("/api/archive/export", params=_params(seeded_archive, admin_token, 5, 25))
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "video/mp4"
    assert "attachment" in r.headers.get("content-disposition", "")

    duration = _probe_duration(r.content, tmp_path)
    # Запрошено 20 с; допуск вверх — на вход по ключевому кадру не позже
    # запрошенного времени (remux не умеет резать точнее, §24).
    assert 19.0 <= duration <= 26.0
    # Фрагмент заметно короче трёх сегментов целиком — то есть рез
    # действительно произошёл, а не «отдали всё, что нашли».
    assert duration < 30.0


def test_export_picks_segment_starting_before_window(
    client, admin_token, seeded_archive, tmp_path
):
    """Сегмент, начавшийся ДО окна, попадает в выборку.

    Отбор по `started_at >= date_from` (как в списке сегментов) потерял бы
    именно тот сегмент, в котором лежит начало фрагмента, и экспорт молча
    начинался бы с середины запрошенного окна.
    """
    # Окно целиком внутри второго сегмента (10..20 с).
    r = client.get("/api/archive/export", params=_params(seeded_archive, admin_token, 12, 18))
    assert r.status_code == 200, r.text
    assert _probe_duration(r.content, tmp_path) > 0


def test_export_window_without_records_is_404(client, admin_token, seeded_archive):
    r = client.get("/api/archive/export", params=_params(seeded_archive, admin_token, 600, 660))
    assert r.status_code == 404


def test_export_rejects_reversed_window(client, admin_token, seeded_archive):
    r = client.get("/api/archive/export", params=_params(seeded_archive, admin_token, 25, 5))
    assert r.status_code == 422


def test_export_rejects_window_over_limit(client, admin_token, seeded_archive):
    """Окно длиннее потолка отклоняется до всякой работы с диском."""
    from app.services import export as export_svc

    r = client.get("/api/archive/export",
                   params=_params(seeded_archive, admin_token, 0,
                                  export_svc.MAX_EXPORT_SECONDS + 60))
    assert r.status_code == 422


def test_export_forbidden_for_viewer(client, make_user, seeded_archive, request):
    """Матрица прав §25: архив — только admin/operator.

    Роль сверяется с БД, а не с claim'ом токена (`require_role_query`).
    """
    _, viewer_token = make_user(f"viewer_{request.node.name}"[:60], "viewer")
    r = client.get("/api/archive/export", params=_params(seeded_archive, viewer_token, 5, 25))
    assert r.status_code == 403


def test_export_requires_token(client, seeded_archive):
    """Без учётных данных — 401 (до цикла 62 было 422: `token` в query был
    обязательным параметром схемы; теперь принимается ещё и заголовок
    `Authorization: Bearer`, см. test_integration_query_token_bearer.py)."""
    r = client.get("/api/archive/export", params={
        "camera_id": seeded_archive,
        "date_from": BASE.isoformat(),
        "date_to": (BASE + timedelta(seconds=25)).isoformat(),
    })
    assert r.status_code == 401


@pytest.fixture()
def _cleanup_audit(pg_conn):
    """Записи журнала, оставленные тестом, убираются за ним (изоляция).

    Имя пользователя уникально на тест, поэтому снимаются ровно свои
    строки — тот же приём, что в `test_audit_role_from_db.py`.
    """
    names = []
    yield names
    if names:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM audit_log WHERE username = ANY(%s)", (names,))


def test_export_is_written_to_audit_log(
    client, make_user, seeded_archive, pg_conn, request, _cleanup_audit
):
    """ТЗ §13: «операции экспорта фиксируются в журнале аудита».

    Экспорт идёт от отдельного пользователя с уникальным именем, а не от
    сидового `admin`: журнал накапливается между прогонами, и проверка «в
    журнале есть строка с таким путём» проходила бы на строке, оставленной
    предыдущим прогоном, — то есть не падала бы и при полностью выключенном
    аудите экспорта. Поймано верификацией откатом.

    Заодно сторож на то, что в журнал не уехал токен из query string: путь
    пишется без неё намеренно (app/audit.py).
    """
    username = f"exp_{request.node.name}"[:60]
    _cleanup_audit.append(username)
    _, token = make_user(username, "operator")  # §25: архив доступен оператору

    r = client.get("/api/archive/export", params=_params(seeded_archive, token, 5, 15))
    assert r.status_code == 200, r.text

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT action, path, role FROM audit_log "
            "WHERE username = %s ORDER BY id DESC LIMIT 1",
            (username,),
        )
        row = cur.fetchone()
    assert row is not None, "экспорт фрагмента не попал в журнал аудита"
    assert row[0] == "Экспорт фрагмента архива"
    assert row[1] == "/api/archive/export"
    assert row[2] == "operator", "роль в журнале должна браться из БД"
    assert token not in row[1], "токен не должен попадать в журнал"


def test_export_skips_segment_missing_on_disk(
    client, admin_token, seeded_archive, pg_conn, tmp_path
):
    """Файл, удалённый ротацией между выборкой и сборкой, пропускается.

    Отказ целиком здесь был бы хуже: фрагмент из оставшихся сегментов
    оператору всё ещё полезен, а ротация по retention (§5) удаляет файлы
    независимо от того, экспортирует ли их кто-то прямо сейчас.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT file_path FROM video_segments WHERE camera_id = %s ORDER BY started_at",
            (seeded_archive,),
        )
        first = cur.fetchone()[0]
    os.remove(first)

    r = client.get("/api/archive/export", params=_params(seeded_archive, admin_token, 5, 25))
    assert r.status_code == 200, r.text
    # Остались второй и третий сегменты — примерно 15 секунд вместо 20.
    assert _probe_duration(r.content, tmp_path) > 0
