"""Снимок в полном разрешении берётся один раз на кадр, а не на каждое лицо.

Находка цикла 19 (P2, SPEC.md раздел 18 «Оптимизация для маломощного
оборудования»). И HTTP-запрос снимка к камере, и прогон детектора по
полноразмерному кадру стояли внутри цикла `for f in faces` в
process_faces(). Кадр с пятью людьми давал пять скачиваний одного и того
же JPEG и пять прогонов тяжёлой модели по одной и той же картинке. На 16
камерах это ровно та нагрузка, которую раздел 18 ТЗ велит избегать, — и
ради избегания которой снимок вообще брался одним GET вместо постоянного
декодирования основного потока.

Тест идёт production path: настоящая сессия SQLAlchemy к настоящему
Postgres с pgvector, настоящий find_or_create_person (включая
pg_advisory_xact_lock из цикла 16), настоящая вставка FaceEvent, настоящий
Redis для publish. Подменяются ровно две вещи, которых в песочнице
существовать не может: сетевой запрос к камере и ML-модель. Обе подмены —
счётчики вызовов, то есть именно то, что проверяется.

Требует полный requirements.txt воркера для импорта worker.py, поэтому в
CI-джобе воркера пропускается (см. .github/workflows/ci.yml и
test_camera_worker_onvif_thread.py — тот же приём importorskip).
"""
import os

import pytest

# Все тяжёлые импорты — строго после importorskip (урок цикла 16: top-level
# import numpy до этой точки ломает graceful skip хардовым ImportError).
worker = pytest.importorskip(
    "worker",
    reason="нужен полный requirements.txt воркера (cv2/numpy/sklearn/pgvector)",
)

import numpy as np  # noqa: E402


class _FakeFace:
    """Лицо в том виде, в каком его отдаёт insightface: bbox + эмбеддинг.

    `ident` задаёт направление вектора: единица в своей координате, ноль в
    остальных. Такие векторы взаимно ортогональны, поэтому разные ident —
    гарантированно разные персоны в find_or_create_person (одинаковой длины
    вектор с разными значениями не годится: после нормировки он даёт одно и
    то же направление, и все лица схлопываются в одну персону).
    """

    def __init__(self, bbox, ident: int):
        self.bbox = np.asarray(bbox, dtype=np.float32)
        vec = np.zeros(512, dtype=np.float32)
        vec[ident % 512] = 1.0
        self.normed_embedding = vec


class _CountingDetector:
    """Модель, считающая свои вызовы."""

    def __init__(self, faces):
        self.faces = faces
        self.calls = 0

    def get(self, frame):
        self.calls += 1
        return self.faces


@pytest.fixture()
def live_services():
    """Пропуск, если Postgres/Redis недоступны (локальный прогон без
    docker-compose), — иначе здоровое дерево давало бы ложное «failed»."""
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


def _seed_camera(cam_id_holder):
    """Камера нужна как внешний ключ FaceEvent.camera_id."""
    with worker.Session() as s:
        cam = worker.Camera(
            name="test-cam-snapshot-reuse",
            rtsp_url_enc="x",
            location="",
            motion_sensitivity=0,
            enabled=False,
            status="offline",
        )
        s.add(cam)
        s.commit()
        cam_id_holder.append(cam.id)
        return cam.id


def _cleanup(cam_id):
    with worker.Session() as s:
        s.execute(worker.delete(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id))
        s.execute(worker.delete(worker.Camera).where(worker.Camera.id == cam_id))
        s.commit()


@pytest.fixture()
def camera(live_services):
    ids = []
    cam_id = _seed_camera(ids)
    try:
        yield cam_id
    finally:
        _cleanup(cam_id)


def test_snapshot_fetched_once_per_frame_not_once_per_face(camera, media_dir, monkeypatch):
    """Три лица в кадре — один HTTP-запрос снимка и один прогон детектора.

    До фикса было бы по три того и другого.
    """
    cam_id = camera
    fetches = []
    hires_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    def _fake_fetch(url, timeout=4.0):
        fetches.append(url)
        return hires_frame

    # Лица на снимке — по одному под каждое лицо кадра аналитики, в тех же
    # относительных позициях (кадр аналитики 640×360, снимок 1920×1080:
    # координаты втрое больше).
    hi_faces = [
        _FakeFace((300, 300, 450, 450), 1),
        _FakeFace((900, 300, 1050, 450), 2),
        _FakeFace((1500, 300, 1650, 450), 3),
    ]
    detector = _CountingDetector(hi_faces)
    monkeypatch.setattr(worker, "fetch_snapshot_frame", _fake_fetch)
    monkeypatch.setattr(worker, "FACE_APP", detector)

    analytics_faces = [
        _FakeFace((100, 100, 150, 150), 1),
        _FakeFace((300, 100, 350, 150), 2),
        _FakeFace((500, 100, 550, 150), 3),
    ]
    frame = np.zeros((360, 640, 3), dtype=np.uint8)

    worker.process_faces(
        cam_id, frame, analytics_faces, 640, 360,
        now=10_000.0, last_event_at={}, snapshot_url="http://camera/snapshot",
    )

    assert len(fetches) == 1, f"снимок скачан {len(fetches)} раз(а) вместо одного"
    assert detector.calls == 1, f"детектор прогнан {detector.calls} раз(а) вместо одного"

    # И при этом события действительно созданы — проверка не выродилась в
    # «ничего не делали, поэтому и не скачивали».
    with worker.Session() as s:
        events = s.execute(
            worker.select(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id)
        ).scalars().all()
    assert len(events) == 3, "на каждое лицо должно быть создано событие"


def test_each_person_gets_its_own_crop(camera, media_dir, monkeypatch):
    """Три лица кадра — три разных кропа со снимка, а не один на всех.

    Общий список лиц снимка (следствие того, что снимок теперь один на кадр)
    без учёта уже занятых отдал бы двум персонам один и тот же кроп и один и
    тот же эмбеддинг — то есть в поиск по фото ушло бы чужое лицо.
    """
    cam_id = camera
    hires_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    monkeypatch.setattr(worker, "fetch_snapshot_frame", lambda url, timeout=4.0: hires_frame)

    hi_faces = [
        _FakeFace((300, 300, 450, 450), 1),
        _FakeFace((330, 300, 480, 450), 2),
    ]
    monkeypatch.setattr(worker, "FACE_APP", _CountingDetector(hi_faces))

    # Два лица кадра аналитики рядом — обоим ближайшим окажется одно и то же
    # лицо снимка, если не отслеживать уже занятые.
    analytics_faces = [
        _FakeFace((100, 100, 150, 150), 1),
        _FakeFace((110, 100, 160, 150), 9),
    ]
    frame = np.zeros((360, 640, 3), dtype=np.uint8)

    worker.process_faces(
        cam_id, frame, analytics_faces, 640, 360,
        now=20_000.0, last_event_at={}, snapshot_url="http://camera/snapshot",
    )

    with worker.Session() as s:
        events = s.execute(
            worker.select(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id)
        ).scalars().all()
        paths = [e.snapshot_path for e in events]
    assert len(paths) == 2
    assert len(set(paths)) == 2, f"обе персоны получили один и тот же кроп: {paths}"


def test_no_snapshot_url_means_no_fetch_attempt(camera, media_dir, monkeypatch):
    """Камера без ONVIF-снимка обрабатывается как раньше — кроп из кадра
    аналитики, ни одного сетевого запроса."""
    cam_id = camera
    fetches = []
    monkeypatch.setattr(worker, "fetch_snapshot_frame",
                        lambda url, timeout=4.0: fetches.append(url))
    monkeypatch.setattr(worker, "FACE_APP", _CountingDetector([]))

    frame = np.zeros((360, 640, 3), dtype=np.uint8)
    worker.process_faces(
        cam_id, frame, [_FakeFace((100, 100, 150, 150), 4)], 640, 360,
        now=30_000.0, last_event_at={}, snapshot_url=None,
    )

    assert fetches == []
    with worker.Session() as s:
        events = s.execute(
            worker.select(worker.FaceEvent).where(worker.FaceEvent.camera_id == cam_id)
        ).scalars().all()
    assert len(events) == 1
    assert events[0].snapshot_path, "кроп из кадра аналитики должен быть сохранён"
