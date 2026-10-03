"""Событие, уходящее на Стену, несёт теги персоны (SPEC §15).

Стена фильтрует живую ленту по тегам, и живое событие приходит к ней по
WebSocket от воркера, а не через `/api/events`: если теги едут только в
REST-ответе, фильтр работает на истории и молча не работает на всём, что
пришло после открытия страницы. Ровно тот класс расхождения между двумя
путями одной функции, из-за которого в цикле 48 бэкап существовал в
docker-compose и отсутствовал в production.

Тест идёт production path: настоящая сессия к настоящему Postgres,
настоящий find_or_create_person, настоящая вставка FaceEvent. Подменяются
две вещи, которых в песочнице быть не может, — сетевой снимок с камеры и
ML-модель, — плюс `r.publish`, потому что проверяется именно его аргумент.

Требует полный requirements.txt воркера, поэтому в лёгкой CI-джобе воркера
пропускается (тот же приём importorskip, что и в
test_process_faces_snapshot_reuse.py).
"""
import json

import pytest

worker = pytest.importorskip(
    "worker",
    reason="нужен полный requirements.txt воркера (cv2/numpy/sklearn/pgvector)",
)

import numpy as np  # noqa: E402


class _FakeFace:
    def __init__(self, bbox, ident: int):
        self.bbox = np.asarray(bbox, dtype=np.float32)
        vec = np.zeros(512, dtype=np.float32)
        vec[ident % 512] = 1.0
        self.normed_embedding = vec


@pytest.fixture()
def live_services():
    try:
        with worker.Session() as s:
            s.execute(worker.text("SELECT 1"))
        worker.r.ping()
    except Exception as e:
        pytest.skip(f"нет живых Postgres/Redis: {e}")


@pytest.fixture()
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "MEDIA_PATH", str(tmp_path))
    (tmp_path / "snapshots").mkdir()
    return tmp_path


@pytest.fixture()
def camera(live_services):
    """Камера теста — и уборка персон, которые на ней завелись.

    Персоны обязаны исчезать вместе с камерой, а не только события:
    find_or_create_person ищет по эмбеддингу, и оставленная размеченная
    персона матчится на следующем прогоне — тест «у новой персоны тегов
    нет» падает на здоровом дереве. Ровно этот класс загрязнения ловил
    сторож изоляции бэкенда в цикле 48; у воркера сторожа нет, поэтому
    уборка здесь явная.

    Персоны снимаются по своим событиям на этой камере: эмбеддинги файла
    (ident 41 и 57) не используются другими тестами, поэтому чужого здесь
    удалить нечего.
    """
    with worker.Session() as s:
        cam = worker.Camera(
            name="test-cam-publishes-tags",
            rtsp_url_enc="x",
            location="",
            motion_sensitivity=0,
            enabled=False,
            status="offline",
        )
        s.add(cam)
        s.commit()
        cam_id = cam.id
    try:
        yield cam_id
    finally:
        with worker.Session() as s:
            pids = [row[0] for row in s.execute(
                worker.select(worker.FaceEvent.person_id)
                .where(worker.FaceEvent.camera_id == cam_id)
                .distinct()
            ).all() if row[0] is not None]
            s.execute(worker.delete(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id))
            if pids:
                s.execute(worker.delete(worker.FaceEvent).where(
                    worker.FaceEvent.person_id.in_(pids)))
                s.execute(worker.delete(worker.Person).where(worker.Person.id.in_(pids)))
            s.execute(worker.delete(worker.Camera).where(worker.Camera.id == cam_id))
            s.commit()


@pytest.fixture()
def published(monkeypatch):
    """Перехват `faces:new`: проверяется именно то, что уходит на Стену."""
    msgs: list[dict] = []

    def _publish(channel, payload):
        if channel == "faces:new":
            msgs.append(json.loads(payload))

    monkeypatch.setattr(worker.r, "publish", _publish)
    return msgs


def _run_one_face(cam_id, monkeypatch, ident: int = 41):
    hires = np.zeros((1080, 1920, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "fetch_snapshot_frame", lambda url, timeout=4.0: hires)

    class _Detector:
        def get(self, frame):
            return [_FakeFace((300, 300, 450, 450), ident)]

    monkeypatch.setattr(worker, "FACE_APP", _Detector())
    worker.process_faces(
        cam_id, np.zeros((360, 640, 3), dtype=np.uint8),
        [_FakeFace((100, 100, 150, 150), ident)], 640, 360,
        now=10_000.0, last_event_at={}, snapshot_url="http://camera/snapshot",
    )


def _tag_person(pid: int, tags: list[str]):
    with worker.Session() as s:
        person = s.get(worker.Person, pid)
        person.tags = tags
        s.commit()


def test_event_payload_carries_person_tags(camera, media_dir, monkeypatch, published):
    """Второе появление уже размеченной персоны публикуется с её тегами.

    Первый прогон заводит персону (у новой тегов и не может быть) —
    размечаем её и прогоняем ещё раз с тем же эмбеддингом, обнуляя
    cooldown: это и есть обычный ход событий на объекте, где карточку
    размечают после первого появления человека.
    """
    _run_one_face(camera, monkeypatch)
    assert published and published[0]["tags"] == [], "у новой персоны тегов нет"

    pid = published[0]["person_id"]
    _tag_person(pid, ["подрядчик", "склад"])
    published.clear()

    _run_one_face(camera, monkeypatch)
    assert published, "второе появление не опубликовано"
    assert published[0]["person_id"] == pid, "распозналась другая персона"
    assert published[0]["tags"] == ["подрядчик", "склад"]


def test_payload_tags_are_always_a_list(camera, media_dir, monkeypatch, published):
    """`tags` — список и у персоны без тегов, а не null.

    Стена зовёт `.includes()` на этом поле: null уронил бы отрисовку
    ленты целиком, а не одну плитку.
    """
    _run_one_face(camera, monkeypatch, ident=57)
    assert published
    assert isinstance(published[0]["tags"], list)
