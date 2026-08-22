"""§24 «возможность удаления данных по требованию» на реальном Postgres.

Проверяется не «эндпоинт ответил 200», а то, ради чего требование
существует: после удаления человека его биометрия не находится **поиском по
фото** — тем самым запросом, которым её нашли бы до удаления
(routers/search.py, ANN_SEARCH_SQL по `face_events.embedding`). Поиск здесь
идёт тем же SQL, что и в production, но без вызова воркера: эмбеддинг
загруженного фото в CI посчитать нечем (сервис распознавания не поднимается,
см. .github/workflows/ci.yml), поэтому вектор запроса подставляется прямо —
подменяется только то, что в проде отдаёт `/embed`.

Верификация откатом (цикл 49): на коде до этого набора
`test_erase_removes_biometrics_from_photo_search` падает — «удалённая»
персона остаётся в выдаче поиска со всеми своими кадрами.
"""
import os

import pytest

from app.config import settings
from app.routers.search import ANN_SEARCH_SQL


def _vec(seed: float = 0.02) -> str:
    return "[" + ",".join(f"{seed:.4f}" for _ in range(512)) + "]"


@pytest.fixture(autouse=True)
def _seeded(pg_conn):
    """Уборка за тестом. Персона под тест обычно исчезает в самом тесте —
    её и стирают, — но упавший до конца тест не должен травить соседей
    (та же причина, что у `_seeded` в test_integration_persons.py)."""
    seeded = {"persons": [], "files": []}
    yield seeded
    with pg_conn.cursor() as cur:
        if seeded["persons"]:
            cur.execute("DELETE FROM face_events WHERE person_id = ANY(%s)", (seeded["persons"],))
            cur.execute("DELETE FROM persons WHERE id = ANY(%s)", (seeded["persons"],))
    for path in seeded["files"]:
        try:
            os.remove(path)
        except OSError:
            pass


def _make_snapshot(seeded, name: str) -> str:
    """Кладёт файл в media/snapshots и возвращает относительный путь —
    ровно в том виде, в каком его пишет воркер (`snapshots/<имя>`)."""
    directory = os.path.join(settings.MEDIA_PATH, "snapshots")
    os.makedirs(directory, exist_ok=True)
    abs_path = os.path.join(directory, name)
    with open(abs_path, "wb") as fh:
        fh.write(b"\xff\xd8\xff\xe0jpeg-stub")
    seeded["files"].append(abs_path)
    return f"snapshots/{name}"


def _seed_person_with_faces(pg_conn, seeded, camera_id: int, tag: str, n: int = 3):
    """Персона + `n` событий с эмбеддингами и файлами снимков.

    Аватар персоны указывает на снимок её первого события — так его
    проставляет воркер (`person.avatar_path = p.snap_rel`), и это тот самый
    случай, в котором один файл принадлежит двум строкам сразу.
    """
    rels = [_make_snapshot(seeded, f"{tag}_{i}.jpg") for i in range(n)]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, avatar_path, centroid, alert_on_detection, created_at) "
            "VALUES (%s, 'unknown', %s, CAST(%s AS vector), false, NOW()) RETURNING id",
            (f"p_{tag}"[:60], rels[0], _vec()),
        )
        pid = cur.fetchone()[0]
        seeded["persons"].append(pid)
        for rel in rels:
            cur.execute(
                "INSERT INTO face_events "
                "(camera_id, person_id, ts, embedding, is_known, enhanced, snapshot_path, orig_snapshot_path) "
                "VALUES (%s, %s, NOW(), CAST(%s AS vector), false, false, %s, %s)",
                (camera_id, pid, _vec(), rel, rel),
            )
    return pid, rels


def _photo_search(pg_conn, threshold: float = 0.9, limit: int = 100):
    """Поиск по фото production-запросом. Возвращает строки выдачи.

    `hnsw.ef_search` роутер поднимает через `SET LOCAL`, то есть на время
    транзакции; здесь соединение в autocommit, где `SET LOCAL` — молчаливый
    no-op, и ставить его значило бы делать вид, что настройка применена.
    На объёмах теста она и не нужна: дефолтных 40 кандидатов хватает на
    десяток строк, а проверяется форма выдачи, а не полнота HNSW (её
    проверяет `test_integration_search_index.py`).
    """
    with pg_conn.cursor() as cur:
        cur.execute(ANN_SEARCH_SQL.replace(":vec", "%(vec)s")
                                  .replace(":limit", "%(limit)s")
                                  .replace(":threshold", "%(threshold)s"),
                    {"vec": _vec(), "limit": limit, "threshold": threshold})
        return cur.fetchall()


def test_erase_removes_biometrics_from_photo_search(client, admin_headers, pg_conn,
                                                    make_camera, request, _seeded):
    """Главная проверка §24: после удаления поиск по фото человека не находит."""
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    pid, rels = _seed_person_with_faces(pg_conn, _seeded, cam, request.node.name[:24])

    before = _photo_search(pg_conn)
    assert any(row[1] == pid for row in before), "сид не найден поиском — тест бессмысленен"

    r = client.delete(f"/api/persons/{pid}/biometrics", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["events_removed"] == len(rels)
    # Файлов ровно три, хотя строк, ссылающихся на них, семь: у каждого
    # события snapshot_path == orig_snapshot_path, а аватар персоны — это
    # первый из тех же снимков.
    assert body["files_removed"] == len(rels), body

    after = _photo_search(pg_conn)
    assert not any(row[1] == pid for row in after)
    # Ни одной осиротевшей строки: person_id IS NULL прошёл бы проверку выше
    # (в выдаче нет `pid`), но сами кадры остались бы в базе и в поиске.
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM face_events WHERE person_id = %s", (pid,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM persons WHERE id = %s", (pid,))
        assert cur.fetchone()[0] == 0
    for rel in rels:
        assert not os.path.exists(os.path.join(settings.MEDIA_PATH, rel))


def test_plain_delete_leaves_biometrics_findable(client, admin_headers, pg_conn,
                                                 make_camera, request, _seeded):
    """Закрепляет ровно тот дефект, ради которого заведён соседний роут.

    `DELETE /api/persons/{pid}` снимает только карточку, а кадры остаются в
    базе и **остаются находимыми поиском по фото** — при том, что диалог в
    интерфейсе до цикла 49 обещал «удалить персону и все её снимки». Тест
    держит две вещи сразу: что неразрушающий путь остался неразрушающим
    (оператор не должен терять архив, убирая карточку) и что он сам по себе
    §24 не закрывает.
    """
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    pid, rels = _seed_person_with_faces(pg_conn, _seeded, cam, request.node.name[:24], n=2)

    r = client.delete(f"/api/persons/{pid}", headers=admin_headers)
    assert r.status_code == 200, r.text

    still = _photo_search(pg_conn)
    found = [row for row in still if row[4] in rels]
    assert len(found) == len(rels), "кадры «удалённой» персоны обязаны остаться в выдаче"
    assert all(row[1] is None for row in found), "их владелец при этом снят"

    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM face_events WHERE person_id IS NULL "
                    "AND snapshot_path = ANY(%s)", (rels,))
        assert cur.fetchone()[0] == len(rels)
    for rel in rels:
        assert os.path.exists(os.path.join(settings.MEDIA_PATH, rel))
        # Уборка теста: строки с person_id IS NULL фикстура по person_id уже
        # не найдёт.
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM face_events WHERE snapshot_path = %s", (rel,))


def test_erase_keeps_file_shared_with_surviving_person(client, admin_headers, pg_conn,
                                                       make_camera, request, _seeded):
    """Файл, на который ссылается выжившая строка, остаётся на диске.

    Так выглядит связь после слияния: события уехали к другой персоне, а
    аватар источника указывает на снимок одного из них. Удаление источника
    не должно выбивать картинку из-под чужой карточки.
    """
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    keeper, keeper_rels = _seed_person_with_faces(pg_conn, _seeded, cam,
                                                  f"keep_{request.node.name}"[:24], n=1)
    shared = keeper_rels[0]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, avatar_path, centroid, alert_on_detection, created_at) "
            "VALUES (%s, 'unknown', %s, CAST(%s AS vector), false, NOW()) RETURNING id",
            (f"gone_{request.node.name}"[:60], shared, _vec()),
        )
        victim = cur.fetchone()[0]
        _seeded["persons"].append(victim)

    r = client.delete(f"/api/persons/{victim}/biometrics", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["files_kept"] == 1
    assert os.path.exists(os.path.join(settings.MEDIA_PATH, shared))
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM face_events WHERE person_id = %s", (keeper,))
        assert cur.fetchone()[0] == 1


def test_erase_is_admin_only(client, make_user, pg_conn, make_camera, request, _seeded):
    """§18: удаление биометрии необратимо, оператору оно недоступно."""
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    pid, _rels = _seed_person_with_faces(pg_conn, _seeded, cam, request.node.name[:24], n=1)
    _uid, token = make_user(f"op_{request.node.name}"[:30], "operator")

    r = client.delete(f"/api/persons/{pid}/biometrics",
                      headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403
    with pg_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM face_events WHERE person_id = %s", (pid,))
        assert cur.fetchone()[0] == 1


def test_erase_unknown_person_is_404(client, admin_headers):
    r = client.delete("/api/persons/99999999/biometrics", headers=admin_headers)
    assert r.status_code == 404


def test_erase_is_written_to_audit_log(client, admin_headers, pg_conn,
                                       make_camera, request, _seeded):
    """§24 требует аудита доступа к биометрии; её удаление — крайний случай
    такого доступа, и в журнале оно обязано отличаться от «Удалена персона»."""
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    pid, _rels = _seed_person_with_faces(pg_conn, _seeded, cam, request.node.name[:24], n=1)

    assert client.delete(f"/api/persons/{pid}/biometrics",
                         headers=admin_headers).status_code == 200

    r = client.get("/api/audit", headers=admin_headers)
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    mine = [i for i in items if i["path"] == f"/api/persons/{pid}/biometrics"]
    assert mine, [i["path"] for i in items[:5]]
    assert "биометри" in mine[0]["action"].lower(), mine[0]
    assert mine[0]["method"] == "DELETE" and mine[0]["status_code"] == 200
