"""§15 «Ручное слияние кластеров»: слияние обязано менять распознавание.

Распознавание сличает новое лицо с `persons.centroid` — им карточка и
узнаётся (`find_or_create_person` в воркере). До цикла 65 ручное слияние
переносило события и теги, а центроид цели не трогало: карточка узнавалась
ровно так же, как до слияния, и появление, попадавшее в слитую карточку,
заводило ТРЕТЬЮ. Оператор сливал её снова — бесконечно.

Проверка идёт по той же арифметике, что у автоматического пути
(`worker.recluster_unknowns`): центроид объединённой карточки = нормированное
среднее эмбеддингов её событий. То есть тест сторожит не «поле изменилось»,
а согласие двух путей слияния.
"""
import math

import pytest


def _vec_text(values) -> str:
    return "[" + ",".join(repr(float(x)) for x in values) + "]"


def _ort(idx: int, dim: int = 512):
    """Единичный вектор вдоль оси `idx` — два таких ортогональны.

    Ортогональные эмбеддинги берутся намеренно: у них среднее считается
    в уме (1/√2 по двум осям), поэтому ожидаемый центроид в тесте —
    записанное заранее число, а не пересчёт той же формулой, что в коде.
    Формула, проверяющая сама себя, зелена при любой ошибке в ней.
    """
    v = [0.0] * dim
    v[idx] = 1.0
    return v


@pytest.fixture()
def seeded(pg_conn):
    made = {"persons": [], "face_events": []}
    yield made
    with pg_conn.cursor() as cur:
        if made["persons"]:
            cur.execute("DELETE FROM face_events WHERE person_id = ANY(%s)", (made["persons"],))
            cur.execute("DELETE FROM persons WHERE id = ANY(%s)", (made["persons"],))


def _person(pg_conn, seeded, centroid) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, centroid, alert_on_detection, created_at) "
            "VALUES ('', 'unknown', CAST(%s AS vector), false, NOW()) RETURNING id",
            (_vec_text(centroid),),
        )
        pid = cur.fetchone()[0]
    seeded["persons"].append(pid)
    return pid


def _event(pg_conn, seeded, camera_id: int, person_id: int, embedding) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, enhanced) "
            "VALUES (%s, %s, NOW(), CAST(%s AS vector), false, false) RETURNING id",
            (camera_id, person_id, _vec_text(embedding)),
        )
        eid = cur.fetchone()[0]
    seeded["face_events"].append(eid)
    return eid


def _centroid_of(pg_conn, pid: int):
    with pg_conn.cursor() as cur:
        cur.execute("SELECT centroid::text FROM persons WHERE id = %s", (pid,))
        row = cur.fetchone()
    assert row is not None, f"персона {pid} исчезла"
    from app.services.person_centroid import parse_vector
    return parse_vector(row[0])


def test_merge_recomputes_centroid_as_cluster_mean(
    client, admin_headers, pg_conn, seeded, make_camera, request
):
    """Центроид цели := нормированное среднее эмбеддингов обеих карточек."""
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    dst = _person(pg_conn, seeded, _ort(0))
    src = _person(pg_conn, seeded, _ort(1))
    _event(pg_conn, seeded, cam, dst, _ort(0))
    _event(pg_conn, seeded, cam, src, _ort(1))

    r = client.post(f"/api/persons/{src}/merge/{dst}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["centroid_recomputed"] is True

    got = _centroid_of(pg_conn, dst)
    # Среднее двух ортогональных единичных векторов, нормированное:
    # по обеим осям 1/√2, остальные координаты нулевые.
    half = 1.0 / math.sqrt(2.0)
    assert got[0] == pytest.approx(half, abs=1e-6)
    assert got[1] == pytest.approx(half, abs=1e-6)
    assert got[2:] == pytest.approx([0.0] * 510, abs=1e-6)
    assert math.sqrt(sum(x * x for x in got)) == pytest.approx(1.0, abs=1e-6)


def test_merge_moves_events_and_drops_source(
    client, admin_headers, pg_conn, seeded, make_camera, request
):
    """Пересчёт центроида не должен ломать то, что слияние делало и раньше."""
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    dst = _person(pg_conn, seeded, _ort(0))
    src = _person(pg_conn, seeded, _ort(1))
    _event(pg_conn, seeded, cam, dst, _ort(0))
    e2 = _event(pg_conn, seeded, cam, src, _ort(1))
    e3 = _event(pg_conn, seeded, cam, src, _ort(1))

    r = client.post(f"/api/persons/{src}/merge/{dst}", headers=admin_headers)
    assert r.status_code == 200, r.text

    with pg_conn.cursor() as cur:
        cur.execute("SELECT person_id FROM face_events WHERE id = ANY(%s)", ([e2, e3],))
        assert [row[0] for row in cur.fetchall()] == [dst, dst]
        cur.execute("SELECT count(*) FROM persons WHERE id = %s", (src,))
        assert cur.fetchone()[0] == 0
        cur.execute("SELECT count(*) FROM face_events WHERE person_id = %s", (dst,))
        assert cur.fetchone()[0] == 3


def test_merge_without_embeddings_keeps_previous_centroid(
    client, admin_headers, pg_conn, seeded, make_camera, request
):
    """Нечем пересчитать — прежний центроид сохраняется, а не затирается.

    У карточки, собранной до появления эмбеддингов (или после того, как
    ротация §5 вынесла её события), `avg()` вернёт NULL. Записать NULL в
    центроид означало бы выкинуть карточку из распознавания совсем —
    отказ хуже исходного дефекта.
    """
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    dst = _person(pg_conn, seeded, _ort(3))
    src = _person(pg_conn, seeded, _ort(4))
    # События есть, но без эмбеддингов — ровно тот случай, что даёт NULL.
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, is_known, enhanced) "
            "VALUES (%s, %s, NOW(), false, false) RETURNING id",
            (cam, src),
        )
        seeded["face_events"].append(cur.fetchone()[0])

    before = _centroid_of(pg_conn, dst)
    r = client.post(f"/api/persons/{src}/merge/{dst}", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["centroid_recomputed"] is False
    assert _centroid_of(pg_conn, dst) == pytest.approx(before, abs=1e-9)


def test_merged_card_now_recognises_the_absorbed_appearance(
    client, admin_headers, pg_conn, seeded, make_camera, request
):
    """Следствие, ради которого правка и сделана (§15).

    Появление, попадавшее в слитую карточку, обязано после слияния
    узнаваться объединённой — иначе оператор будет сливать третью
    карточку, четвёртую и так далее. Проверяется тем же запросом, которым
    воркер ищет персону (`ORDER BY centroid <=> ...`), и тем же порогом
    по умолчанию (0.45).
    """
    cam = make_camera(f"cam_{request.node.name}"[:60])["id"]
    # Два близких, но различимых направления: расстояние между ними
    # больше порога, поэтому ДО слияния появление src-типа в dst не
    # попадает, а после — попадает в их среднее.
    a = _ort(0)
    b = [0.0] * 512
    b[0], b[1] = 0.3, math.sqrt(1 - 0.3 ** 2)
    dst = _person(pg_conn, seeded, a)
    src = _person(pg_conn, seeded, b)
    _event(pg_conn, seeded, cam, dst, a)
    _event(pg_conn, seeded, cam, src, b)

    def distance_to(pid, probe):
        with pg_conn.cursor() as cur:
            cur.execute(
                "SELECT centroid <=> CAST(%s AS vector) FROM persons WHERE id = %s",
                (_vec_text(probe), pid),
            )
            return cur.fetchone()[0]

    threshold = 0.45
    before = distance_to(dst, b)
    assert before > threshold, (
        f"подготовка теста неверна: появление уже узнаётся целью ({before})")

    r = client.post(f"/api/persons/{src}/merge/{dst}", headers=admin_headers)
    assert r.status_code == 200, r.text

    after = distance_to(dst, b)
    assert after < threshold, (
        f"§15: после слияния появление по-прежнему не узнаётся объединённой "
        f"карточкой (расстояние {after} при пороге {threshold}) — оператор "
        f"получит третью карточку того же человека")
    assert after < before
