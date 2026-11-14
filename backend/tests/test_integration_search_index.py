"""Поиск по фото должен идти через HNSW-индекс (SPEC §12, §26).

Индекс `idx_face_events_embedding` создаётся при старте приложения
(main.py) с самого начала, но запрос из `routers/search.py` его не
использовал: порядок был задан выражением-псевдонимом
(`ORDER BY similarity DESC`), а планировщик pgvector отдаёт индекс только
форме `ORDER BY <столбец> <=> <константа>`. Замерено на 100 000
эмбеддингов (`perf/bench.py --only facesearch`): 517 мс и Seq Scan против
2.0 мс и Index Scan.

Норматив §26 (≤ 3–5 с) выполнялся и на Seq Scan — поэтому одной проверкой
времени этот пробел не ловится: она была бы зелёной. Ловится он планом
запроса и полнотой выдачи, чем эти тесты и заняты.

База засевается тысячами строк: на десятке строк планировщик выберет
последовательный проход просто потому, что он дешевле, и проверка плана
стала бы ложно-зелёной на любой реализации.
"""
import io
import random

import httpx
import pytest

from app.routers import search as search_mod

# Достаточно, чтобы планировщик предпочёл индекс, и заметно больше
# дефолтного hnsw.ef_search (40) — иначе урезанная выдача была бы
# неотличима от полной.
SEED_ROWS = 3000
DIM = 512
THRESHOLD = 0.5


def _unit(rnd):
    v = [rnd.gauss(0.0, 1.0) for _ in range(DIM)]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def _near(rnd, c, spread=0.005):
    """Вектор рядом с `c`. Разброс мал намеренно: все засеянные строки
    должны уверенно проходить порог схожести, иначе тест на полноту
    выдачи мерил бы не полноту, а разброс."""
    v = [x + rnd.gauss(0.0, spread) for x in c]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def _lit(v):
    return "[" + ",".join(f"{x:.5f}" for x in v) + "]"


@pytest.fixture(scope="module")
def big_archive(request):
    """Камера, персона и SEED_ROWS событий с эмбеддингами вокруг одного
    центроида. Модульная область видимости: засев стоит секунды, и делать
    его на каждый тест незачем."""
    import psycopg2
    from urllib.parse import urlsplit

    from app.config import settings

    parsed = urlsplit(settings.DATABASE_URL.replace("+asyncpg", ""))
    try:
        conn = psycopg2.connect(
            host=parsed.hostname, port=parsed.port or 5432,
            user=parsed.username, password=parsed.password,
            dbname=parsed.path.lstrip("/"), connect_timeout=5,
        )
    except Exception as e:
        pytest.skip(f"Реальный Postgres недоступен: {e}")
    conn.autocommit = True

    rnd = random.Random(4242)
    centroid = _unit(rnd)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('idx-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO persons (name, status, alert_on_detection, created_at) "
            "VALUES ('Индексная персона', 'known', false, NOW()) RETURNING id"
        )
        person_id = cur.fetchone()[0]

        buf = io.StringIO()
        for _ in range(SEED_ROWS):
            buf.write(f"{cam_id}\t{person_id}\t2026-08-01 00:00:00\t"
                      f"{_lit(_near(rnd, centroid))}\tt\tf\n")
        buf.seek(0)
        cur.copy_from(buf, "face_events",
                      columns=("camera_id", "person_id", "ts", "embedding",
                               "is_known", "enhanced"))
        cur.execute("ANALYZE face_events")

    def _cleanup():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM face_events WHERE camera_id = %s", (cam_id,))
            cur.execute("DELETE FROM persons WHERE id = %s", (person_id,))
            cur.execute("DELETE FROM cameras WHERE id = %s", (cam_id,))
        conn.close()

    request.addfinalizer(_cleanup)
    return {"conn": conn, "camera_id": cam_id, "person_id": person_id,
            "centroid": centroid}


def _to_psycopg(sql: str) -> str:
    """`:name` (SQLAlchemy) → `%(name)s` (psycopg2).

    Нужно затем, чтобы EXPLAIN шёл по САМОЙ константе из `search.py`, а не
    по её копии в тесте. Копия делала тест ложно-зелёным: он оставался
    зелёным при любой правке production-запроса, в том числе при возврате
    к форме, которая индекс не использует. Поймано верификацией откатом.
    """
    import re

    return re.sub(r":(\w+)", r"%(\1)s", sql)


def test_unfiltered_search_uses_hnsw_index(big_archive, client):
    """План поиска по всей базе не должен содержать Seq Scan по face_events.

    `client` в аргументах — чтобы приложение успело создать
    `idx_face_events_embedding` (main.py:lifespan) до того, как мы
    посмотрим план.
    """
    qvec = _lit(big_archive["centroid"])
    with big_archive["conn"].cursor() as cur:
        cur.execute(f"SET LOCAL hnsw.ef_search = {search_mod._ef_search(100)}")
        cur.execute(
            "EXPLAIN " + _to_psycopg(search_mod.ANN_SEARCH_SQL),
            {"vec": qvec, "limit": 100, "threshold": THRESHOLD},
        )
        plan = "\n".join(r[0] for r in cur.fetchall())

    assert "Seq Scan on face_events" not in plan, (
        "поиск по фото идёт последовательным проходом по всей таблице — "
        f"HNSW-индекс не используется. План:\n{plan}"
    )
    assert "Index Scan using idx_face_events_embedding" in plan, (
        f"ожидался индексный скан по idx_face_events_embedding. План:\n{plan}"
    )


def test_search_returns_full_page_not_ef_search_default(
    big_archive, client, admin_headers, monkeypatch
):
    """Выдача не должна обрезаться дефолтным `hnsw.ef_search` = 40.

    Это и есть ловушка перехода на индекс: он молча отдаёт не более
    `ef_search` кандидатов, а дефолт (40) МЕНЬШЕ дефолтного `limit`
    эндпоинта (100). Оператор увидел бы 40 совпадений и решил, что
    похожих больше нет.

    Проверяется через настоящий эндпоинт: подменён только сетевой вызов к
    воркеру за эмбеддингом, сам поиск идёт по настоящей БД.
    """
    centroid = big_archive["centroid"]

    class _Resp:
        def json(self):
            return {"ok": True, "embedding": centroid}

    async def _fake_post(self, url, **kwargs):
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        data={"threshold": str(THRESHOLD), "limit": "100"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    results = r.json()
    assert len(results) == 100, (
        f"запрошено 100 совпадений, получено {len(results)}. "
        "Похоже на дефолтный hnsw.ef_search = 40: индекс отдаёт не больше "
        "ef_search кандидатов, и выдача урезается молча."
    )
    # Сортировка по убыванию схожести сохранилась при переходе на CTE.
    sims = [item["similarity"] for item in results]
    assert sims == sorted(sims, reverse=True)
    assert all(s >= THRESHOLD for s in sims), "порог схожести перестал применяться"


def test_narrowed_search_stays_exact(big_archive, client, admin_headers, monkeypatch):
    """Суженный фильтром поиск идёт точным путём и не теряет совпадения.

    На узком фильтре ANN отдал бы `ef_search` ближайших и отфильтровал уже
    их — совпадение за пределами первых кандидатов пропало бы. Здесь
    фильтр по статусу совпадает с засеянной персоной, поэтому выдача
    обязана быть непустой.
    """
    centroid = big_archive["centroid"]

    class _Resp:
        def json(self):
            return {"ok": True, "embedding": centroid}

    async def _fake_post(self, url, **kwargs):
        return _Resp()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_post)

    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        data={"threshold": str(THRESHOLD), "limit": "100", "status": "known"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    results = r.json()
    assert results, "суженный поиск потерял совпадения"
    assert all(item["status"] == "known" for item in results)


def test_ef_search_never_below_limit():
    """`ef_search` не должен опускаться ниже запрошенного числа строк."""
    assert search_mod._ef_search(1) == 40      # нижняя граница — дефолт pgvector
    assert search_mod._ef_search(100) == 200
    assert search_mod._ef_search(100) >= 100
    # Потолок pgvector не превышается даже на максимальном limit.
    assert search_mod._ef_search(500) == 1000
    assert search_mod._ef_search(10_000) == 1000
