"""Шкала архива через настоящий эндпоинт (ТЗ §5).

`test_archive_timeline.py` проверяет склейку покрытия; здесь — production
path: настоящий Postgres, настоящий роутер, настоящая проверка роли по БД.
Именно этот слой отвечает на вопрос, ради которого шкала и появилась, —
**в какие минуты у камеры есть запись, и где перерывы**, — а также ловит
то, чего не видно на уровне сервиса: отбор по пересечению с окном, границы
параметров и матрицу прав §18.
"""
from datetime import datetime, timedelta

import pytest

from app.services import archive_timeline as tl

DAY = datetime(2026, 4, 7, 0, 0, 0)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


@pytest.fixture
def seeded_camera(pg_conn, request):
    """Камера и запись суток с одним настоящим перерывом.

    03:00–03:15 тремя пятиминутными сегментами встык, затем двадцать минут
    без записи (обрыв RTSP), затем 03:35–03:40. Файлов на диске нет
    намеренно: шкала строится по индексу `video_segments`, и заводить
    четыре MP4 значило бы проверять не то.
    """
    spans = [
        (timedelta(hours=3), timedelta(hours=3, minutes=5)),
        (timedelta(hours=3, minutes=5), timedelta(hours=3, minutes=10)),
        (timedelta(hours=3, minutes=10), timedelta(hours=3, minutes=15)),
        (timedelta(hours=3, minutes=35), timedelta(hours=3, minutes=40)),
    ]
    seg_ids = []
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('timeline-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        for i, (a, b) in enumerate(spans):
            cur.execute(
                "INSERT INTO video_segments "
                "(camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes) "
                "VALUES (%s, %s, %s, %s, 'continuous', %s, 0) RETURNING id",
                (cam_id, DAY + a, DAY + b, f"/media/segments/timeline_{i}.mp4",
                 int((b - a).total_seconds())),
            )
            seg_ids.append(cur.fetchone()[0])

    def _cleanup():
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM video_segments WHERE camera_id = %s", (cam_id,))
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))

    request.addfinalizer(_cleanup)
    return cam_id


def _get(client, headers, cam_id, hours_from=0, hours_to=24):
    return client.get("/api/archive/timeline", headers=headers, params={
        "camera_id": cam_id,
        "date_from": _iso(DAY + timedelta(hours=hours_from)),
        "date_to": _iso(DAY + timedelta(hours=hours_to)),
    })


def test_timeline_shows_continuous_recording_and_the_real_break(
    client, admin_headers, seeded_camera
):
    """Главная проверка §5: три сегмента встык — одна полоса, перерыв — виден.

    До этой шкалы «есть ли запись в 03:20» читалось только перебором строк
    таблицы, а отсутствие строки не видно в списке вовсе: дыра — это то,
    чего нет.
    """
    r = _get(client, admin_headers, seeded_camera)
    assert r.status_code == 200, r.text
    body = r.json()

    assert len(body["ranges"]) == 2, body["ranges"]
    first, second = body["ranges"]
    assert first["start"].startswith("2026-04-07T03:00:00")
    assert first["end"].startswith("2026-04-07T03:15:00")
    assert second["start"].startswith("2026-04-07T03:35:00")
    assert second["end"].startswith("2026-04-07T03:40:00")

    # 15 минут + 5 минут; сумма считается по склеенным диапазонам.
    assert body["recorded_sec"] == pytest.approx(20 * 60)
    assert body["truncated"] is False


def test_segments_come_in_playback_order(client, admin_headers, seeded_camera):
    """Цепочка воспроизведения — по возрастанию времени.

    Выдача таблицы архива идёт от свежих к старым; шкала обязана идти
    наоборот, иначе плеер после первого файла ушёл бы назад во времени.
    """
    body = _get(client, admin_headers, seeded_camera).json()
    starts = [s["started_at"] for s in body["segments"]]
    assert starts == sorted(starts)
    assert len(starts) == 4
    # Путь к файлу наружу не отдаётся — плееру он не нужен.
    assert "file_path" not in body["segments"][0]


def test_segment_started_before_the_window_is_not_lost(
    client, admin_headers, seeded_camera
):
    """Отбор по пересечению, а не по началу сегмента.

    Окно с 03:07: сегмент 03:05–03:10 начался раньше окна, но содержит его
    начало. Фильтр «started_at >= date_from» потерял бы его, и шкала
    показала бы дыру там, где запись идёт.
    """
    r = client.get("/api/archive/timeline", headers=admin_headers, params={
        "camera_id": seeded_camera,
        "date_from": _iso(DAY + timedelta(hours=3, minutes=7)),
        "date_to": _iso(DAY + timedelta(hours=3, minutes=12)),
    })
    body = r.json()
    assert len(body["segments"]) == 2
    # Диапазон обрезан границами окна, а не выдан целиком.
    assert body["ranges"][0]["start"].startswith("2026-04-07T03:07:00")
    assert body["ranges"][0]["end"].startswith("2026-04-07T03:12:00")
    assert body["recorded_sec"] == pytest.approx(5 * 60)


def test_camera_without_segments_gives_empty_timeline(client, admin_headers, pg_conn):
    """Пустая шкала — это 200 с пустыми диапазонами, а не 404.

    Камера, заведённая минуту назад, ещё ничего не записала, и страница
    обязана показать пустые сутки, а не ошибку.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('timeline-empty', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
    try:
        body = _get(client, admin_headers, cam_id).json()
        assert body["ranges"] == []
        assert body["segments"] == []
        assert body["recorded_sec"] == 0
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))


def test_window_longer_than_a_day_is_refused(client, admin_headers, seeded_camera):
    """Окно шире суток — 422, а не выдача на тысячи сегментов."""
    r = _get(client, admin_headers, seeded_camera, 0, 25)
    assert r.status_code == 422
    assert "часов" in r.json()["detail"]


def test_inverted_window_is_refused(client, admin_headers, seeded_camera):
    r = client.get("/api/archive/timeline", headers=admin_headers, params={
        "camera_id": seeded_camera,
        "date_from": _iso(DAY + timedelta(hours=5)),
        "date_to": _iso(DAY + timedelta(hours=4)),
    })
    assert r.status_code == 422


def test_camera_id_must_be_positive(client, admin_headers):
    r = client.get("/api/archive/timeline", headers=admin_headers, params={
        "camera_id": 0, "date_from": _iso(DAY), "date_to": _iso(DAY + timedelta(hours=1)),
    })
    assert r.status_code == 422


def test_viewer_cannot_read_the_timeline(client, make_user, seeded_camera):
    """Матрица прав §18: архив — admin/operator, наблюдателю закрыт.

    Шкала перечисляет, когда именно объект писался, — это тот же архив,
    только в другой форме, и роль обязана проверяться так же.
    """
    _, token = make_user("timeline-viewer", "viewer")
    r = _get(client, {"Authorization": f"Bearer {token}"}, seeded_camera)
    assert r.status_code == 403


def test_operator_can_read_the_timeline(client, make_user, seeded_camera):
    """Позитивный контроль к предыдущему: 403 не должен быть у всех подряд."""
    _, token = make_user("timeline-operator", "operator")
    r = _get(client, {"Authorization": f"Bearer {token}"}, seeded_camera)
    assert r.status_code == 200


def test_truncated_is_reported_not_hidden(client, admin_headers, pg_conn):
    """Упор в потолок сегментов помечается флагом.

    Молча обрезанная шкала выглядит как «дальше записи нет» — то есть
    врёт ровно про то, ради чего её и смотрят. Сутки минутными сегментами
    (1440) в потолок не упираются, поэтому строки заводятся секундные:
    проверяется флаг, а не правдоподобие записи.
    """
    over = tl.MAX_TIMELINE_SEGMENTS + 5
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('timeline-many', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO video_segments "
            "(camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes) "
            "SELECT %s, %s::timestamp + (n || ' seconds')::interval, "
            "       %s::timestamp + ((n + 1) || ' seconds')::interval, "
            "       '/media/segments/many_' || n || '.mp4', 'continuous', 1, 0 "
            "FROM generate_series(0, %s) AS n",
            (cam_id, DAY, DAY, over - 1),
        )
    try:
        body = _get(client, admin_headers, cam_id).json()
        assert body["truncated"] is True
        assert len(body["segments"]) == tl.MAX_TIMELINE_SEGMENTS
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM video_segments WHERE camera_id = %s", (cam_id,))
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))


def test_exactly_at_the_cap_is_not_reported_as_truncated(
    client, admin_headers, pg_conn
):
    """Позитивный контроль к флагу: ровно потолок — это не обрезка.

    Без запроса на одну строку больше эти два случая неразличимы, и флаг
    зажигался бы на полной выдаче.
    """
    exact = tl.MAX_TIMELINE_SEGMENTS
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('timeline-exact', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO video_segments "
            "(camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes) "
            "SELECT %s, %s::timestamp + (n || ' seconds')::interval, "
            "       %s::timestamp + ((n + 1) || ' seconds')::interval, "
            "       '/media/segments/exact_' || n || '.mp4', 'continuous', 1, 0 "
            "FROM generate_series(0, %s) AS n",
            (cam_id, DAY, DAY, exact - 1),
        )
    try:
        body = _get(client, admin_headers, cam_id).json()
        assert len(body["segments"]) == exact
        assert body["truncated"] is False
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM video_segments WHERE camera_id = %s", (cam_id,))
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))
