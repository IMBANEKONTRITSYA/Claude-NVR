"""Аватар персоны переживает retention (SPEC §15; цикл 53) — production path.

Решения проверяет `test_avatar_store.py` (только stdlib, идёт и в лёгкой
джобе). Здесь — что они **подключены** к `process_faces`: настоящая
сессия SQLAlchemy к настоящему Postgres с pgvector, настоящий
`find_or_create_person`, настоящая вставка `FaceEvent`, настоящий Redis
для publish. Подменяются ровно две вещи, которых в песочнице быть не
может: сетевой запрос снимка к камере и ML-модель.

Ровно этого разделения не хватало прежнему коду: назначение аватара
никаким тестом не покрывалось вовсе, и то, что путь ведёт в каталог,
который чистится по возрасту, ничем не ловилось.

Требует полный requirements.txt воркера, поэтому в лёгкой CI-джобе
пропускается (тот же приём, что в `test_process_faces_snapshot_reuse.py`).
"""
import os

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
    with worker.Session() as s:
        cam = worker.Camera(name="test-cam-avatar", rtsp_url_enc="x", location="",
                            motion_sensitivity=0, enabled=False, status="offline")
        s.add(cam)
        s.commit()
        cam_id = cam.id
    try:
        yield cam_id
    finally:
        with worker.Session() as s:
            pids = [
                pid for (pid,) in s.execute(
                    worker.select(worker.FaceEvent.person_id).where(
                        worker.FaceEvent.camera_id == cam_id)
                ).all() if pid
            ]
            s.execute(worker.delete(worker.FaceEvent).where(
                worker.FaceEvent.camera_id == cam_id))
            if pids:
                s.execute(worker.delete(worker.Person).where(
                    worker.Person.id.in_(pids)))
            s.execute(worker.delete(worker.Camera).where(worker.Camera.id == cam_id))
            s.commit()


def _see_a_face(cam_id):
    """Один проход распознавания: лицо в кадре аналитики, снимка нет.

    Направление вектора задаётся `cam_id`, а не константой: БД у набора
    общая и переживает прогон, а `find_or_create_person` ищет по близости
    эмбеддинга — с фиксированным вектором тест подхватывал бы персону,
    заведённую предыдущим прогоном, вместе с её `avatar_path`, который
    указывает в чужой (уже удалённый) tmp_path. Идентификатор камеры
    уникален в пределах базы, поэтому каждый прогон получает свою персону.
    """
    faces = [_FakeFace((100, 100, 150, 150), cam_id)]
    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    worker.process_faces(cam_id, frame, faces, 640, 360,
                         now=10_000.0, last_event_at={}, snapshot_url=None)
    with worker.Session() as s:
        return s.execute(
            worker.select(worker.Person)
            .join(worker.FaceEvent, worker.FaceEvent.person_id == worker.Person.id)
            .where(worker.FaceEvent.camera_id == cam_id)
        ).scalars().first()


def test_the_avatar_lands_outside_the_directory_retention_eats(camera, media_dir,
                                                               monkeypatch):
    """Главное: путь ведёт в `avatars/`, а не в `snapshots/`.

    `snapshots/` чистится по возрасту вместе с историей событий, и до
    цикла 53 карточка теряла фото через retention_days — навсегда, потому
    что аватар назначается только когда его «нет», а он был, просто вёл в
    никуда.
    """
    person = _see_a_face(camera)

    assert person is not None and person.avatar_path
    assert person.avatar_path.startswith("avatars/"), person.avatar_path
    assert os.path.exists(os.path.join(str(media_dir), person.avatar_path))


def test_the_snapshot_behind_the_event_is_not_moved_away(camera, media_dir):
    """Копия, а не перенос: на тот же файл ссылается
    `face_events.snapshot_path`, и лента распознавания (§15) не должна
    ломаться ради аватара."""
    _see_a_face(camera)

    with worker.Session() as s:
        ev = s.execute(worker.select(worker.FaceEvent).where(
            worker.FaceEvent.camera_id == camera)).scalars().first()
    assert ev.snapshot_path.startswith("snapshots/")
    assert os.path.exists(os.path.join(str(media_dir), ev.snapshot_path))


def test_a_card_whose_snapshot_retention_ate_gets_a_new_avatar(camera, media_dir):
    """Самолечение карточек, заведённых до цикла 53: ссылка на удалённый
    снимок считается отсутствующим аватаром, и следующая же встреча с
    человеком возвращает ему фото."""
    person = _see_a_face(camera)
    # Приводим карточку к состоянию «до цикла 53»: аватар — ссылка в
    # snapshots/, а файла уже нет (его съел retention).
    with worker.Session() as s:
        p = s.get(worker.Person, person.id)
        p.avatar_path = "snapshots/cam1_long_gone.jpg"
        s.commit()

    _see_a_face(camera)

    with worker.Session() as s:
        healed = s.get(worker.Person, person.id).avatar_path
    assert healed.startswith("avatars/"), healed
    assert os.path.exists(os.path.join(str(media_dir), healed))


def test_a_working_avatar_is_not_replaced_on_every_frame(camera, media_dir):
    """Обратная сторона самолечения: рабочий аватар перезаписывать незачем,
    иначе каждый кадр плодил бы копию файла."""
    person = _see_a_face(camera)
    first = person.avatar_path

    _see_a_face(camera)
    _see_a_face(camera)

    with worker.Session() as s:
        assert s.get(worker.Person, person.id).avatar_path == first
    assert len(os.listdir(os.path.join(str(media_dir), "avatars"))) == 1
