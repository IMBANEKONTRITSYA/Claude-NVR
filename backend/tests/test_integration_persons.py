"""Интеграционные тесты /api/persons на реальном Postgres+Redis (см.
conftest.py:client, pg_conn). Создание персоны через API требует живого
worker'а для эмбеддинга (недоступен в CI — см. .github/workflows/ci.yml),
поэтому персоны/события для чтения/обновления/удаления сидятся напрямую
через pg_conn (тот же путь, что использует сам роутер внутри создания:
raw SQL INSERT с CAST(... AS vector)); недоступность worker'а проверяется
отдельно (503), а полный успешный путь создания — через monkeypatch
httpx.AsyncClient.post (подменяет только сетевой вызов к воркеру, вся
остальная логика — реальная БД)."""
import httpx


def _vec(seed: float = 0.01) -> str:
    return "[" + ",".join(f"{seed:.4f}" for _ in range(512)) + "]"


def _insert_person(pg_conn, name: str, status: str = "known") -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO persons (name, status, centroid, alert_on_detection, created_at) "
            "VALUES (%s, %s, CAST(%s AS vector), false, NOW()) RETURNING id",
            (name, status, _vec()),
        )
        return cur.fetchone()[0]


def _insert_face_event(pg_conn, camera_id: int, person_id: int | None) -> int:
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO face_events (camera_id, person_id, ts, embedding, is_known, enhanced) "
            "VALUES (%s, %s, NOW(), CAST(%s AS vector), %s, false) RETURNING id",
            (camera_id, person_id, _vec(), person_id is not None),
        )
        return cur.fetchone()[0]


def _make_camera(client, admin_headers, request) -> int:
    name = f"cam_{request.node.name}"[:60]
    r = client.post(
        "/api/cameras",
        json={"name": name, "rtsp_url": "rtsp://cam/stream"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_persons_list_requires_auth(client):
    r = client.get("/api/persons")
    assert r.status_code == 401


def test_persons_crud_update_and_delete(client, admin_headers, pg_conn, request):
    pid = _insert_person(pg_conn, f"person_{request.node.name}"[:60])

    r = client.get("/api/persons", headers=admin_headers)
    assert r.status_code == 200
    assert any(p["id"] == pid for p in r.json()["items"])

    r = client.get(f"/api/persons/{pid}", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["status"] == "known"

    r = client.patch(f"/api/persons/{pid}", json={"notes": "проверка"}, headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["notes"] == "проверка"

    r = client.delete(f"/api/persons/{pid}", headers=admin_headers)
    assert r.status_code == 200

    r = client.get(f"/api/persons/{pid}", headers=admin_headers)
    assert r.status_code == 404


def test_persons_get_missing_returns_404(client, admin_headers):
    r = client.get("/api/persons/999999999", headers=admin_headers)
    assert r.status_code == 404


def test_persons_list_filters_by_status_and_query(client, admin_headers, pg_conn, request):
    unique_name = f"findme_{request.node.name}"[:60]
    pid_known = _insert_person(pg_conn, unique_name, status="known")
    pid_unknown = _insert_person(pg_conn, f"other_{request.node.name}"[:60], status="unknown")

    r = client.get("/api/persons", params={"status": "known"}, headers=admin_headers)
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["items"]]
    assert pid_known in ids
    assert pid_unknown not in ids

    r = client.get("/api/persons", params={"q": unique_name}, headers=admin_headers)
    assert r.status_code == 200
    ids = [p["id"] for p in r.json()["items"]]
    assert ids == [pid_known]


def test_persons_update_status_and_alert_flag(client, admin_headers, pg_conn, request):
    pid = _insert_person(pg_conn, f"upd_{request.node.name}"[:60], status="unknown")

    r = client.patch(
        f"/api/persons/{pid}",
        json={"name": "Новое имя", "status": "known", "alert_on_detection": True},
        headers=admin_headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "Новое имя"
    assert body["status"] == "known"
    assert body["alert_on_detection"] is True


def test_persons_update_missing_returns_404(client, admin_headers):
    r = client.patch("/api/persons/999999999", json={"notes": "x"}, headers=admin_headers)
    assert r.status_code == 404


def test_persons_delete_requires_admin_not_operator(client, admin_headers, pg_conn, request):
    username = f"op_{request.node.name}"[:60]
    r = client.post(
        "/api/users",
        json={"username": username, "password": "Str0ngPass!23", "role": "operator"},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    r = client.post("/api/auth/login", data={"username": username, "password": "Str0ngPass!23"})
    op_headers = {"Authorization": f"Bearer {r.json()['access_token']}"}

    pid = _insert_person(pg_conn, f"person_{request.node.name}"[:60])
    r = client.delete(f"/api/persons/{pid}", headers=op_headers)
    assert r.status_code == 403

    r = client.delete(f"/api/persons/{pid}", headers=admin_headers)
    assert r.status_code == 200


def test_persons_merge_reassigns_events_and_deletes_source(client, admin_headers, pg_conn, request):
    cam_id = _make_camera(client, admin_headers, request)
    src_id = _insert_person(pg_conn, f"src_{request.node.name}"[:60])
    dst_id = _insert_person(pg_conn, f"dst_{request.node.name}"[:60])
    ev_id = _insert_face_event(pg_conn, cam_id, src_id)

    r = client.post(f"/api/persons/{src_id}/merge/{dst_id}", headers=admin_headers)
    assert r.status_code == 200

    r = client.get(f"/api/persons/{src_id}", headers=admin_headers)
    assert r.status_code == 404

    r = client.get(f"/api/persons/{dst_id}/gallery", headers=admin_headers)
    assert r.status_code == 200
    assert any(item["id"] == ev_id for item in r.json())


def test_persons_merge_with_self_rejected(client, admin_headers, pg_conn, request):
    pid = _insert_person(pg_conn, f"self_{request.node.name}"[:60])
    r = client.post(f"/api/persons/{pid}/merge/{pid}", headers=admin_headers)
    assert r.status_code == 400


def test_persons_gallery_empty_for_unknown_person(client, admin_headers):
    r = client.get("/api/persons/999999999/gallery", headers=admin_headers)
    assert r.status_code == 200
    assert r.json() == []


def test_persons_enhance_queues_upscale_jobs(client, admin_headers, pg_conn, request):
    cam_id = _make_camera(client, admin_headers, request)
    pid = _insert_person(pg_conn, f"enh_{request.node.name}"[:60])
    _insert_face_event(pg_conn, cam_id, pid)
    _insert_face_event(pg_conn, cam_id, pid)

    r = client.post(f"/api/persons/{pid}/enhance", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["queued"] == 2


def test_persons_enhance_missing_person_404(client, admin_headers):
    r = client.post("/api/persons/999999999/enhance", headers=admin_headers)
    assert r.status_code == 404


def test_create_person_rejects_empty_name(client, admin_headers):
    r = client.post(
        "/api/persons",
        data={"name": "  "},
        files={"file": ("photo.jpg", b"not-empty", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_create_person_rejects_empty_file(client, admin_headers):
    r = client.post(
        "/api/persons",
        data={"name": "Кто-то"},
        files={"file": ("photo.jpg", b"", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 400


def test_create_person_degrades_gracefully_without_worker(client, admin_headers):
    """В CI сервис распознавания (worker) не поднимается — POST должен
    вернуть понятную 503, а не 500/зависание, когда WORKER_URL недоступен."""
    r = client.post(
        "/api/persons",
        data={"name": "Кто-то"},
        files={"file": ("photo.jpg", b"not-empty-bytes", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 503


class _FakeEmbedResponse:
    def json(self):
        return {"ok": True, "embedding": [0.02] * 512}


async def _fake_worker_post(self, url, **kwargs):
    return _FakeEmbedResponse()


def test_create_person_success_path_with_mocked_worker(client, admin_headers, monkeypatch, request):
    """Подменяет только сетевой вызов к worker'у (недоступен в CI) — сохранение
    аватара, INSERT персоны с centroid-вектором и ответ API идут через
    настоящую БД, как в проде."""
    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_worker_post)
    name = f"Реальная персона {request.node.name}"[:60]

    r = client.post(
        "/api/persons",
        data={"name": name},
        files={"file": ("photo.jpg", b"fake-jpeg-bytes", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == name
    assert body["status"] == "known"
    assert body["avatar_path"].startswith("avatars/")

    r = client.get(f"/api/persons/{body['id']}", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["name"] == name


def test_create_person_returns_422_when_worker_reports_failure(client, admin_headers, monkeypatch):
    class _FailResponse:
        def json(self):
            return {"ok": False, "error": "лицо не найдено"}

    async def _fake_fail_post(self, url, **kwargs):
        return _FailResponse()

    monkeypatch.setattr(httpx.AsyncClient, "post", _fake_fail_post)
    r = client.post(
        "/api/persons",
        data={"name": "Без лица"},
        files={"file": ("photo.jpg", b"fake-jpeg-bytes", "image/jpeg")},
        headers=admin_headers,
    )
    assert r.status_code == 422
