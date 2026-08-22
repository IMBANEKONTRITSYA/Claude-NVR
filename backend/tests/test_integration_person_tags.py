"""Теги персон (SPEC §15) на настоящем Postgres — production path.

Юнит-тесты нормализации лежат отдельно (test_person_tags_unit.py); здесь
проверяется то, что без БД проверить нельзя: фильтр по тегу идёт через
`tags @> ARRAY[...]` по GIN-индексу, справочник считает персон, слияние
переносит разметку, а роут `/api/persons/tags` не перехватывается
шаблоном `/{pid}`.

Персоны сидятся напрямую через pg_conn — по той же причине, что и в
test_integration_persons.py: создание через API требует живого воркера
для эмбеддинга.
"""
import pytest

from app.services.person_tags import MAX_TAG_LEN, MAX_TAGS_PER_PERSON


def _vec(seed: float = 0.01) -> str:
    return "[" + ",".join(f"{seed:.4f}" for _ in range(512)) + "]"


@pytest.fixture(autouse=True)
def _seeded(pg_conn):
    """Уборка засеянных строк: теги видны в общем справочнике
    /api/persons/tags, и оставленная персона исказила бы счётчики
    следующего прогона по той же БД."""
    seeded: dict[str, list[int]] = {"persons": []}
    yield seeded
    with pg_conn.cursor() as cur:
        if seeded["persons"]:
            cur.execute("DELETE FROM face_events WHERE person_id = ANY(%s)", (seeded["persons"],))
            cur.execute("DELETE FROM persons WHERE id = ANY(%s)", (seeded["persons"],))


def _insert_person(pg_conn, name: str, tags: list[str] | None = None, _seeded=None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, centroid, alert_on_detection, tags, created_at) "
            "VALUES (%s, 'known', CAST(%s AS vector), false, %s, NOW()) RETURNING id",
            (name, _vec(), tags if tags is not None else []),
        )
        pid = cur.fetchone()[0]
    if _seeded is not None:
        _seeded["persons"].append(pid)
    return pid


def _tag(request, suffix: str = "") -> str:
    """Тег, уникальный для теста: справочник общий на всю базу."""
    return f"t-{request.node.name}{suffix}".lower()[:MAX_TAG_LEN]


# --------------------------------------------------------------- запись

def test_tags_roundtrip_through_patch(client, admin_headers, pg_conn, request, _seeded):
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], _seeded=_seeded)
    tag = _tag(request)

    r = client.patch(f"/api/persons/{pid}", json={"tags": [tag.upper(), f"  {tag}  "]},
                     headers=admin_headers)
    assert r.status_code == 200
    # Канонизация и дедупликация — на записи, а не на чтении.
    assert r.json()["tags"] == [tag]

    assert client.get(f"/api/persons/{pid}", headers=admin_headers).json()["tags"] == [tag]
    items = client.get("/api/persons", params={"q": f"p_{request.node.name}"[:60]},
                       headers=admin_headers).json()["items"]
    assert items[0]["tags"] == [tag]


def test_patch_without_tags_key_keeps_them(client, admin_headers, pg_conn, request, _seeded):
    """`tags: None` — «не трогать», и это не то же самое, что пустой список.

    Иначе любая правка имени или заметки стирала бы разметку персоны.
    """
    tag = _tag(request)
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], [tag], _seeded=_seeded)

    r = client.patch(f"/api/persons/{pid}", json={"notes": "правка заметки"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["tags"] == [tag]


def test_empty_list_clears_tags(client, admin_headers, pg_conn, request, _seeded):
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], [_tag(request)], _seeded=_seeded)
    r = client.patch(f"/api/persons/{pid}", json={"tags": []}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["tags"] == []


def test_person_without_tags_reads_as_empty_list_not_null(client, admin_headers, pg_conn, request, _seeded):
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], _seeded=_seeded)
    assert client.get(f"/api/persons/{pid}", headers=admin_headers).json()["tags"] == []


def test_oversized_input_is_refused_with_400(client, admin_headers, pg_conn, request, _seeded):
    """Отказ, а не усечение: оператор должен увидеть причину."""
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], _seeded=_seeded)

    r = client.patch(f"/api/persons/{pid}", json={"tags": ["x" * (MAX_TAG_LEN + 1)]},
                     headers=admin_headers)
    assert r.status_code == 400
    assert str(MAX_TAG_LEN) in r.json()["detail"]

    r = client.patch(f"/api/persons/{pid}",
                     json={"tags": [f"тег{i}" for i in range(MAX_TAGS_PER_PERSON + 1)]},
                     headers=admin_headers)
    assert r.status_code == 400

    # Отказ ничего не записал.
    assert client.get(f"/api/persons/{pid}", headers=admin_headers).json()["tags"] == []


# --------------------------------------------------------------- фильтр

def test_list_filters_by_tag(client, admin_headers, pg_conn, request, _seeded):
    tag = _tag(request)
    tagged = _insert_person(pg_conn, f"yes_{request.node.name}"[:60], [tag], _seeded=_seeded)
    other = _insert_person(pg_conn, f"no_{request.node.name}"[:60], ["прочий"], _seeded=_seeded)

    ids = [p["id"] for p in client.get("/api/persons", params={"tag": tag},
                                       headers=admin_headers).json()["items"]]
    assert ids == [tagged]
    assert other not in ids


def test_tag_filter_is_case_insensitive(client, admin_headers, pg_conn, request, _seeded):
    """Фильтр канонизирует значение так же, как запись.

    Без этого «VIP» в фильтре давал бы пустой список вместо персон с
    тегом «vip» — молчаливый отказ, самый дорогой класс дефекта в этом
    интерфейсе.
    """
    tag = _tag(request)
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], [tag], _seeded=_seeded)

    for probe in (tag.upper(), f"  {tag} "):
        ids = [p["id"] for p in client.get("/api/persons", params={"tag": probe},
                                           headers=admin_headers).json()["items"]]
        assert ids == [pid], probe


def test_tag_filter_combines_with_status(client, admin_headers, pg_conn, request, _seeded):
    tag = _tag(request)
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], [tag], _seeded=_seeded)
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE persons SET status = 'unknown' WHERE id = %s", (pid,))

    assert client.get("/api/persons", params={"tag": tag, "status": "known"},
                      headers=admin_headers).json()["items"] == []
    ids = [p["id"] for p in client.get("/api/persons", params={"tag": tag, "status": "unknown"},
                                       headers=admin_headers).json()["items"]]
    assert ids == [pid]


def test_unknown_tag_gives_empty_page_with_zero_total(client, admin_headers):
    body = client.get("/api/persons", params={"tag": "нет-такого-тега-нигде"},
                      headers=admin_headers).json()
    assert body["items"] == [] and body["total"] == 0


def test_tag_filter_is_indexable(pg_conn):
    """Фильтр `tags @> ARRAY[...]` умеет идти по idx_persons_tags.

    Проверяется применимость индекса к оператору, а не выбор планировщика.
    Разница существенная. Замер цикла 49 на 100 000 персон: по индексу
    1.24-1.31 мс, перебором 23.7-41.9 мс — то есть индекс нужен; но по
    модели стоимости эти два плана там почти неразличимы (≈2800 против
    ≈3100), и выбор между ними переворачивается от плотности мёртвых
    строк в таблице. Утверждение «планировщик выбрал индекс» было бы
    поэтому ложно-красным на здоровом дереве — оно зависит от истории
    вставок и удалений, а не от кода.

    Применимость же не зависит ни от чего, кроме класса операторов
    индекса, и ломается ровно от того, ради чего тест написан: снесённого
    индекса, сменившегося opclass или перехода роутера на неиндексируемое
    выражение вроде `lower(unnest(tags))`. Функциональные тесты выше этого
    не заметят — ответ у перебора тот же самый.
    """
    marker = "planprobe"
    with pg_conn.cursor() as cur:
        try:
            # centroid оставлен NULL: 512-мерный вектор на строку нужен
            # распознаванию, а плану — нет, и без него сидинг мгновенный.
            cur.execute(
                "INSERT INTO persons (name, status, alert_on_detection, tags, created_at) "
                "SELECT %s || i, 'known', false, "
                "       CASE WHEN i %% 100 = 0 THEN ARRAY['planprobe-rare'] "
                "            ELSE ARRAY['planprobe-common'] END, NOW() "
                "FROM generate_series(1, 5000) AS i",
                (marker,),
            )
            cur.execute("ANALYZE persons")
            cur.execute("SET enable_seqscan = off")
            cur.execute(
                "EXPLAIN SELECT id FROM persons WHERE tags @> ARRAY['planprobe-rare']::text[]")
            plan = "\n".join(row[0] for row in cur.fetchall())
        finally:
            cur.execute("SET enable_seqscan = on")
            cur.execute("DELETE FROM persons WHERE name LIKE %s", (marker + "%",))
            cur.execute("ANALYZE persons")
    assert "idx_persons_tags" in plan, plan


# ----------------------------------------------------------- справочник

def test_tags_catalog_counts_persons(client, admin_headers, pg_conn, request, _seeded):
    tag = _tag(request)
    _insert_person(pg_conn, f"a_{request.node.name}"[:60], [tag], _seeded=_seeded)
    _insert_person(pg_conn, f"b_{request.node.name}"[:60], [tag], _seeded=_seeded)
    _insert_person(pg_conn, f"c_{request.node.name}"[:60], [], _seeded=_seeded)

    rows = client.get("/api/persons/tags", headers=admin_headers).json()
    found = [r for r in rows if r["tag"] == tag]
    assert found == [{"tag": tag, "count": 2}]


def test_tags_catalog_route_is_not_shadowed_by_pid(client, admin_headers):
    """`/api/persons/tags` объявлен до `/{pid}`.

    При обратном порядке FastAPI разобрал бы «tags» как pid и ответил 422
    — справочник исчез бы, а фильтр в интерфейсе остался бы пустым.
    """
    r = client.get("/api/persons/tags", headers=admin_headers)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_tags_catalog_requires_auth(client):
    assert client.get("/api/persons/tags").status_code == 401


# -------------------------------------------------------------- слияние

def test_merge_keeps_tags_of_both_cards(client, admin_headers, pg_conn, request, _seeded):
    """Слияние кластеров не теряет разметку (SPEC §15).

    До этой правки теги источника уходили вместе с удаляемой строкой —
    молча, и обнаружилось бы это на фильтре днями позже.
    """
    src_tag, dst_tag = _tag(request, "-src"), _tag(request, "-dst")
    src = _insert_person(pg_conn, f"src_{request.node.name}"[:60], [src_tag], _seeded=_seeded)
    dst = _insert_person(pg_conn, f"dst_{request.node.name}"[:60], [dst_tag], _seeded=_seeded)

    assert client.post(f"/api/persons/{src}/merge/{dst}", headers=admin_headers).status_code == 200

    assert client.get(f"/api/persons/{dst}", headers=admin_headers).json()["tags"] == sorted(
        [src_tag, dst_tag])
    assert client.get(f"/api/persons/{src}", headers=admin_headers).status_code == 404


# ------------------------------------------------------------ лента §15

def test_events_carry_person_tags(client, admin_headers, pg_conn, request, _seeded, make_camera):
    """Стена фильтрует ленту по тегам, значит событие их несёт."""
    tag = _tag(request)
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], [tag], _seeded=_seeded)
    cam_id = make_camera(f"cam_{request.node.name}"[:60])["id"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, enhanced) "
            "VALUES (%s, %s, NOW(), CAST(%s AS vector), true, false) RETURNING id",
            (cam_id, pid, _vec()),
        )
        eid = cur.fetchone()[0]

    events = client.get("/api/events", params={"limit": 50}, headers=admin_headers).json()
    mine = [e for e in events if e["id"] == eid]
    assert mine and mine[0]["tags"] == [tag]


def test_event_without_person_has_empty_tags(client, admin_headers, pg_conn, request, make_camera):
    """Событие без персоны отдаёт [], а не null: Стена зовёт .includes()."""
    cam_id = make_camera(f"cam_{request.node.name}"[:60])["id"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, enhanced) "
            "VALUES (%s, NULL, NOW(), CAST(%s AS vector), false, false) RETURNING id",
            (cam_id, _vec()),
        )
        eid = cur.fetchone()[0]
    try:
        events = client.get("/api/events", params={"limit": 50}, headers=admin_headers).json()
        mine = [e for e in events if e["id"] == eid]
        assert mine and mine[0]["tags"] == []
    finally:
        with pg_conn.cursor() as cur:
            cur.execute("DELETE FROM face_events WHERE id = %s", (eid,))


# ------------------------------------------------------------ права §18

def test_viewer_cannot_read_or_edit_tags(client, make_user_headers, pg_conn, request, _seeded):
    """«Карточки персон» в матрице §18 — админ и оператор, наблюдателю нет."""
    pid = _insert_person(pg_conn, f"p_{request.node.name}"[:60], _seeded=_seeded)
    viewer = make_user_headers(f"viewer_{request.node.name}"[:60], "viewer")

    assert client.get("/api/persons/tags", headers=viewer).status_code == 403
    assert client.get("/api/persons", params={"tag": "vip"}, headers=viewer).status_code == 403
    assert client.patch(f"/api/persons/{pid}", json={"tags": ["vip"]},
                        headers=viewer).status_code == 403
