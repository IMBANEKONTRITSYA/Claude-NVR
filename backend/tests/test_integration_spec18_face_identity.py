"""Матрица прав §18 на данных распознавания — наблюдатель и «Карточки персон».

Находка цикла 51. §18 перечисляет пятнадцать действий, и **каждое**, которое
относится к модулю распознавания лиц §15, закрыто для наблюдателя:

    | Поиск по фото (аналитика) | Да | Да | **Нет** |
    | Карточки персон           | Да | Да | **Нет** |
    | Ручной апскейл лица       | Да | Да | **Нет** |

Наблюдателю §18 оставляет ровно два раздела — «Просмотр видео онлайн» и
«Дашборд и мониторинг». Ни одной строки, разрешающей ему данные §15, в
матрице нет.

При этом «Стена распознавания» (§15) отдавалась наблюдателю целиком:
`/api/events` возвращает имя персоны, путь к кадру лица и её теги (включая
watchlist), `/api/stats/top-persons` — рейтинг названных по именам людей за
30 дней, а `/api/media/snapshots|avatars` — сами фотографии. Роут
`/api/persons` при этом наблюдателю отвечал 403. То есть карточка персон
была заперта с фасада, а её содержимое выдавалось через соседнюю дверь:
имя, фотография и watchlist-разметка — это и есть карточка.

Прежнее обоснование (`main.py`, `MEDIA_KIND_ROLES`) держалось на посылке
«Стена и дашборд разрешены наблюдателю той же матрицей». Посылка неверна:
строки «Стена» в §18 нет вовсе, а §9 перечисляет содержимое дашборда
поимённо — «количество событий, активность по часам, **топ камер**»,
— и персон среди него не значится.

**Граница, которую эти тесты держат с двух сторон.** §4 прямо разрешает
наблюдателю живой просмотр «bounding box'ов вокруг лиц **с подписями**», а
строка «Просмотр видео онлайн» §18 даёт ему это право. Поэтому `/ws/faces`
наблюдателю не закрывается — он несёт геометрию рамки для живого наложения
(LiveGrid). Закрывается то, что рамке не нужно и что принадлежит карточке:
`snapshot_path` (хранимый кадр) и `tags` (watchlist). Тесты проверяют оба
края: наблюдатель по-прежнему получает рамку и подпись, но не получает
хранимый кадр и разметку.

Production path: настоящий Postgres, настоящий вход через /api/auth/login,
настоящий HTTP через TestClient — не вызов обработчика с синтетическим
токеном.
"""
import pytest

pytestmark = pytest.mark.usefixtures("client")


def _vec(seed: float = 0.02) -> str:
    return "[" + ",".join(f"{seed:.4f}" for _ in range(512)) + "]"


@pytest.fixture()
def seeded_face(pg_conn, request):
    """Персона с именем, watchlist-тегом и событием лица со снимком.

    Строки сидятся напрямую: создание персоны через API требует живого
    воркера для эмбеддинга (та же причина, что в test_integration_persons.py).
    """
    name = f"spec18_{request.node.name}"[:60]
    tag = "watchlist-spec18"
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, centroid, alert_on_detection, tags, created_at) "
            "VALUES (%s, 'known', CAST(%s AS vector), true, %s, NOW()) RETURNING id",
            (name, _vec(), [tag]),
        )
        pid = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, mode, enabled, status) "
            "VALUES (%s, 'stub-enc', '', 'analytics', true, 'offline') RETURNING id",
            (f"cam_{request.node.name}"[:60],),
        )
        cam_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, "
            "enhanced, snapshot_path) "
            "VALUES (%s, %s, NOW(), CAST(%s AS vector), true, false, %s) RETURNING id",
            (cam_id, pid, _vec(), "cam1_face.jpg"),
        )
        eid = cur.fetchone()[0]
    yield {"person_id": pid, "camera_id": cam_id, "event_id": eid, "name": name, "tag": tag}
    with pg_conn.cursor() as cur:
        cur.execute("DELETE FROM face_events WHERE id = %s", (eid,))
        cur.execute("DELETE FROM persons WHERE id = %s", (pid,))
        cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))


# ------------------------------------------------- лента распознавания §15

def test_viewer_cannot_read_face_event_feed(client, make_user_headers, seeded_face):
    """Ядро находки: лента Стены — это карточки персон, выданные списком.

    До фикса наблюдатель получал 200 и в ответе — имя персоны, путь к её
    фотографии и watchlist-теги, хотя /api/persons тому же наблюдателю
    отвечает 403.
    """
    headers = make_user_headers("spec18_feed_viewer", "viewer")
    r = client.get("/api/events", params={"limit": 50}, headers=headers)
    assert r.status_code == 403, r.text


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_analytics_roles_still_read_face_event_feed(client, make_user_headers, seeded_face, role):
    """Строка «Карточки персон: Да/Да» — фикс не должен ломать Стену для тех,
    кому она разрешена."""
    headers = make_user_headers(f"spec18_feed_{role}", role)
    r = client.get("/api/events", params={"limit": 100}, headers=headers)
    assert r.status_code == 200, r.text
    mine = [e for e in r.json() if e["id"] == seeded_face["event_id"]]
    assert mine, "событие должно быть видно роли, которой Стена разрешена"
    assert mine[0]["name"] == seeded_face["name"]
    assert mine[0]["tags"] == [seeded_face["tag"]]


# ------------------------------------------------------- дашборд §9 vs §18

def test_viewer_cannot_read_top_persons(client, make_user_headers, seeded_face):
    """§9 перечисляет содержимое дашборда поимённо и персон среди него нет:
    «количество событий, активность по часам, топ камер».

    Рейтинг названных по именам людей за 30 дней — это карточки персон,
    отсортированные по частоте, а не KPI дашборда.
    """
    headers = make_user_headers("spec18_top_viewer", "viewer")
    r = client.get("/api/stats/top-persons", headers=headers)
    assert r.status_code == 403, r.text


def test_viewer_keeps_aggregate_dashboard(client, make_user_headers):
    """Обратная сторона: строка «Дашборд и мониторинг: Да/Да/**Да**» обязана
    продолжать работать для наблюдателя.

    Если бы фикс закрыл дашборд целиком, он нарушил бы §18 ровно так же,
    как нарушала его прежняя выдача персон, — только в другую сторону.
    Эти четыре ручки агрегатные: в них нет ни одного имени.
    """
    headers = make_user_headers("spec18_dash_viewer", "viewer")
    for path in ("/api/stats/kpi", "/api/stats/by-day", "/api/stats/by-hour",
                 "/api/stats/heatmap"):
        r = client.get(path, headers=headers)
        assert r.status_code == 200, f"{path}: {r.text}"


# --------------------------------------------------------- фотографии лиц

def test_viewer_cannot_read_face_snapshots(client, make_user_headers, tmp_path, monkeypatch):
    """Сама фотография лица — тоже содержимое карточки.

    Прежде каталоги snapshots/avatars были открыты всем ролям «осознанно»
    (комментарий в main.py) — на посылке, что на них построена разрешённая
    наблюдателю Стена. Стена ему не разрешена, посылка отпала.
    """
    from app.config import settings

    monkeypatch.setattr(settings, "MEDIA_PATH", str(tmp_path))
    for kind in ("snapshots", "avatars"):
        d = tmp_path / kind
        d.mkdir()
        (d / "cam1_face.jpg").write_bytes(b"stub")

    _, token = _user(client, make_user_headers, "spec18_media_viewer", "viewer")
    for kind in ("snapshots", "avatars"):
        r = client.get(f"/api/media/{kind}/cam1_face.jpg?token={token}")
        assert r.status_code == 403, f"{kind}: {r.text}"


@pytest.mark.parametrize("role", ["admin", "operator"])
def test_analytics_roles_still_read_face_snapshots(client, make_user_headers, tmp_path,
                                                   monkeypatch, role):
    from app.config import settings

    monkeypatch.setattr(settings, "MEDIA_PATH", str(tmp_path))
    d = tmp_path / "snapshots"
    d.mkdir()
    (d / "cam1_face.jpg").write_bytes(b"stub")

    _, token = _user(client, make_user_headers, f"spec18_media_{role}", role)
    r = client.get(f"/api/media/snapshots/cam1_face.jpg?token={token}")
    assert r.status_code == 200, r.text
    assert r.content == b"stub"


def _user(client, make_user_headers, name: str, role: str):
    """Токен строкой (для «ссылочных» эндпоинтов с token в query)."""
    headers = make_user_headers(name, role)
    return None, headers["Authorization"].split(" ", 1)[1]
