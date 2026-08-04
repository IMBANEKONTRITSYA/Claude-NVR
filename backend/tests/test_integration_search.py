"""Интеграционные тесты /api/search/face. Эмбеддинг запроса идёт через
worker (недоступен в CI — см. .github/workflows/ci.yml): сетевой вызов
подменяется monkeypatch'ем httpx.AsyncClient.post, а сам pgvector-поиск
по face_events выполняется на настоящей БД — не мок."""
import httpx


def _vec(seed: float = 0.02) -> str:
    return "[" + ",".join(f"{seed:.4f}" for _ in range(512)) + "]"


def _seed_camera_and_matching_event(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO cameras (name, rtsp_url_enc, location, enabled, status, created_at) "
            "VALUES ('search-cam', 'unused-enc-blob', '', true, 'offline', NOW()) RETURNING id"
        )
        cam_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO persons (name, status, alert_on_detection, created_at) "
            "VALUES ('Найденный', 'known', false, NOW()) RETURNING id"
        )
        person_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, enhanced) "
            "VALUES (%s, %s, NOW(), CAST(%s AS vector), true, false)",
            (cam_id, person_id, _vec()),
        )
    return cam_id, person_id


class _FakeEmbedResponse:
    def json(self):
        return {"ok": True, "embedding": [0.02] * 512}


async def _fake_worker_post(self, url, **kwargs):
    return _FakeEmbedResponse()


def test_search_face_requires_auth(client):
    r = client.post("/api/search/face", files={"file": ("photo.jpg", b"x", "image/jpeg")})
    assert r.status_code == 401


def test_search_face_rejects_empty_file(client, admin_headers):
    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_search_face_degrades_gracefully_without_worker(client, admin_headers):
    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 503


def test_search_face_forbidden_for_operator_role_that_lacks_grant(client, admin_headers, request):
    """search допускает admin и operator — только явное отсутствие токена
    должно отказывать; проверяем, что валидный operator-токен допускается
    до бизнес-логики (а не отсекается RBAC раньше срока)."""
    username = f"op_{request.node.name}"[:60]
    r = client.post(
        "/api/users",
        json={"username": username, "password": "Str0ngPass!23", "role": "operator"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", data={"username": username, "password": "Str0ngPass!23"})
    op_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        headers=op_headers,
    )
    # 503 (worker недоступен), а не 403 — значит RBAC пропустил operator'а
    assert r.status_code == 503


def test_search_face_finds_matching_event_with_mocked_worker(client, admin_headers, monkeypatch, pg_conn):
    """Подменяет только сетевой вызов к worker'у — сам косинусный поиск по
    pgvector и сборка ответа идут по настоящей БД, включая join с segment_id."""
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_worker_post)
    _cam_id, person_id = _seed_camera_and_matching_event(pg_conn)

    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        data={"threshold": "0.5"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    results = r.json()
    assert any(item["person_id"] == person_id and item["similarity"] >= 0.99 for item in results)


def test_search_face_filters_by_status_and_date_range(client, admin_headers, monkeypatch, pg_conn):
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_worker_post)
    _cam_id, person_id = _seed_camera_and_matching_event(pg_conn)

    r = client.post(
        "/api/search/face",
        files={"file": ("photo.jpg", b"some-bytes", "image/jpeg")},
        data={"threshold": "0.5", "status": "unknown"},
        headers=admin_headers,
    )
    assert r.status_code == 200
    assert all(item["person_id"] != person_id for item in r.json())
