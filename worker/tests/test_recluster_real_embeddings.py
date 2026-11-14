"""Кластеризация неизвестных (§15) на НАСТОЯЩИХ эмбеддингах.

Почему этого набора не было до цикла 65 и почему синтетика его не
заменяет. `recluster_unknowns()` существует с первых циклов, но проверялся
только на эмбеддингах вида `vec[ident % 512] = 1.0` (см.
`test_process_faces_avatar.py`). Такие вектора строго ортогональны:
косинусное расстояние между любыми двумя — ровно 1.0, то есть **вдвое
дальше** `DBSCAN_EPS = 0.35`. На них DBSCAN не сливает ничего и не может
слить ничего, какой бы ни был eps, — то есть путь слияния не исполнялся
ни разу, а порог не проверялся вовсе.

Здесь берутся настоящие 512-мерные эмбеддинги настоящей `buffalo_s` с
настоящих лиц (`data/real_face_embeddings.json`, снято с `t1.jpg` из
состава insightface — шесть человек, по шесть вариантов каждого). Истинная
разметка известна, поэтому проверяется не только «слилось», но и «слилось
правильно»: кластер не должен смешать двух разных людей.

Состояние, с которого начинается тест, — то, которое и возникает на
объекте: порог схожести выставлен туго (§15 разрешает 0.1-0.9), одна и та
же личность заводится несколькими персонами, и §15 обещает собрать их
обратно кластеризацией.
"""
import json
import os
import pathlib

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)
np = pytest.importorskip("numpy")

FIXTURE = pathlib.Path(__file__).parent / "data" / "real_face_embeddings.json"


@pytest.fixture(scope="module")
def real_embeddings():
    if not FIXTURE.exists():
        pytest.skip(f"нет фикстуры настоящих эмбеддингов: {FIXTURE}")
    data = json.loads(FIXTURE.read_text())
    assert data["dim"] == 512, "эмбеддинги проекта — 512D (§15)"
    recs = data["records"]
    assert len({r["identity"] for r in recs}) >= 2, "нужно хотя бы две личности"
    return recs


@pytest.fixture()
def live_services():
    try:
        with worker.Session() as s:
            s.execute(worker.text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"нет живого Postgres: {e}")


@pytest.fixture()
def fragmented(live_services, real_embeddings):
    """Заводит по персоне на каждый вектор — раздробленное состояние.

    Именно так выглядит объект с туго выставленным порогом: шесть человек,
    тридцать шесть персон. Камера своя, чтобы уборка не трогала чужие
    строки: `recluster_unknowns()` смотрит на всех неизвестных сразу, и
    набор обязан быть терпим к посторонним персонам в базе.
    """
    created = []
    with worker.Session() as s:
        cam = worker.Camera(name="test-cam-recluster", rtsp_url_enc="x", location="",
                            motion_sensitivity=0, enabled=False, status="offline")
        s.add(cam)
        s.commit()
        cam_id = cam.id

    # Засев — внутри try: камера уже зафиксирована, и упади засев посередине
    # (так и было 2026-10-03 на NOT NULL is_known), уборка без try не
    # запускалась бы вовсе. Протёкшие камеры с шифротекстом 'x' затем
    # роняли выгрузку камер в наборах бэкенда на той же базе.
    try:
        with worker.Session() as s:
            for rec in real_embeddings:
                emb = np.asarray(rec["embedding"], dtype=np.float32)
                p = worker.Person(name="", status="unknown", centroid=emb.tolist(),
                                  created_at=worker.datetime.utcnow())
                s.add(p)
                s.flush()
                ev = worker.FaceEvent(camera_id=cam_id, person_id=p.id,
                                      ts=worker.datetime.utcnow(), embedding=emb.tolist(),
                                      is_known=False, enhanced=False)
                s.add(ev)
                created.append({"person_id": p.id, "identity": rec["identity"]})
            s.commit()

        yield {"cam_id": cam_id, "created": created}
    finally:
        with worker.Session() as s:
            # Персоны, выжившие после слияния, — не те, что заводились,
            # поэтому собираются по событиям камеры, а не по списку.
            pids = {pid for (pid,) in s.execute(
                worker.select(worker.FaceEvent.person_id).where(
                    worker.FaceEvent.camera_id == cam_id)).all() if pid}
            s.execute(worker.delete(worker.FaceEvent).where(
                worker.FaceEvent.camera_id == cam_id))
            if pids:
                s.execute(worker.delete(worker.Person).where(worker.Person.id.in_(pids)))
            s.execute(worker.delete(worker.Camera).where(worker.Camera.id == cam_id))
            s.commit()


def _surviving(cam_id):
    """Персоны, на которые смотрят события нашей камеры, с их событиями."""
    with worker.Session() as s:
        rows = s.execute(
            worker.select(worker.FaceEvent.id, worker.FaceEvent.person_id).where(
                worker.FaceEvent.camera_id == cam_id)).all()
    by_person = {}
    for ev_id, pid in rows:
        by_person.setdefault(pid, []).append(ev_id)
    return by_person


def test_fragmented_identities_are_merged_back(fragmented, real_embeddings):
    """36 персон одного и того же шестёрки людей сходятся обратно в 6."""
    cam_id = fragmented["cam_id"]
    truth = len({r["identity"] for r in real_embeddings})

    before = _surviving(cam_id)
    assert len(before) == len(real_embeddings), "ожидалось раздробленное состояние"

    worker.recluster_unknowns()

    after = _surviving(cam_id)
    assert len(after) == truth, (
        f"кластеризация свела {len(before)} персон к {len(after)}, "
        f"а настоящих людей в источнике {truth}"
    )


def test_no_events_are_lost_on_merge(fragmented):
    """Слияние переносит события, а не удаляет их вместе с персоной."""
    cam_id = fragmented["cam_id"]
    total_before = sum(len(v) for v in _surviving(cam_id).values())
    worker.recluster_unknowns()
    total_after = sum(len(v) for v in _surviving(cam_id).values())
    assert total_after == total_before, (
        f"событий было {total_before}, осталось {total_after} — слияние теряет историю"
    )


def test_clusters_do_not_mix_different_people(fragmented, real_embeddings):
    """Порог не настолько широк, чтобы склеить двух разных людей.

    Это обратная сторона предыдущей проверки: сойтись к шести можно и
    случайно. Здесь сверяется истинная разметка — в каждом кластере один
    человек. Проверка ловит слишком большой `DBSCAN_EPS`, который иначе
    выглядел бы как «кластеризация работает ещё лучше».
    """
    cam_id = fragmented["cam_id"]
    identity_of_person = {c["person_id"]: c["identity"] for c in fragmented["created"]}
    # Событие -> истинная личность: снимается до слияния, пока person_id
    # ещё указывает на заведённую под этот вектор персону.
    with worker.Session() as s:
        rows = s.execute(
            worker.select(worker.FaceEvent.id, worker.FaceEvent.person_id).where(
                worker.FaceEvent.camera_id == cam_id)).all()
    identity_of_event = {ev_id: identity_of_person[pid] for ev_id, pid in rows}

    worker.recluster_unknowns()

    for pid, ev_ids in _surviving(cam_id).items():
        idents = {identity_of_event[e] for e in ev_ids}
        assert len(idents) == 1, (
            f"персона {pid} собрала события разных людей: {sorted(idents)} — "
            f"DBSCAN_EPS={worker.DBSCAN_EPS} слишком велик"
        )


def test_centroid_is_normalized_after_merge(fragmented):
    """Центроид кластера остаётся единичным: по нему идёт поиск (§15)."""
    cam_id = fragmented["cam_id"]
    worker.recluster_unknowns()
    pids = list(_surviving(cam_id))
    with worker.Session() as s:
        rows = s.execute(worker.text(
            "SELECT id, centroid FROM persons WHERE id = ANY(:ids)"
        ), {"ids": pids}).all()
    assert rows, "персоны после слияния должны существовать"
    for pid, centroid in rows:
        vec = worker._to_vec(centroid)
        assert vec.shape == (512,), f"персона {pid}: центроид не 512D"
        norm = float(np.linalg.norm(vec))
        assert abs(norm - 1.0) < 1e-3, (
            f"персона {pid}: норма центроида {norm:.4f}, а сравнение идёт "
            "по косинусу — ненормированный центроид смещает порог"
        )


def test_synthetic_onehot_embeddings_would_not_catch_this(real_embeddings):
    """Почему набор стоит на настоящих векторах, а не на синтетических.

    Проверка-обоснование, а не тавтология: она фиксирует, что прежняя
    синтетика физически не могла проверить слияние, и роняет набор, если
    кто-то решит заменить фикстуру на удобные ортогональные вектора.
    """
    a = np.zeros(512, dtype=np.float32); a[1] = 1.0
    b = np.zeros(512, dtype=np.float32); b[2] = 1.0
    assert float(1 - a @ b) == pytest.approx(1.0), "одно-горячие вектора ортогональны"
    assert 1.0 > worker.DBSCAN_EPS, "то есть заведомо дальше порога слияния"

    # У настоящих эмбеддингов одного человека расстояние внутри порога.
    by_ident = {}
    for r in real_embeddings:
        by_ident.setdefault(r["identity"], []).append(np.asarray(r["embedding"], dtype=np.float32))
    same = [1 - float(v[0] @ v[i]) for v in by_ident.values() if len(v) > 1 for i in range(1, len(v))]
    assert same, "в фикстуре должны быть несколько вариантов одной личности"
    assert min(same) < worker.DBSCAN_EPS, (
        f"ближайшая пара одного человека на расстоянии {min(same):.3f}, "
        f"а eps={worker.DBSCAN_EPS} — фикстура не даёт слиянию случиться"
    )
