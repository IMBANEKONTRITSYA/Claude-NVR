"""Фильтр ленты лиц по роли — юнит-тесты без Postgres, Redis и FastAPI.

Набор намеренно не требует ничего, кроме stdlib: решение живёт в
`services/face_feed_acl.py` именно для того, чтобы проверяться и в лёгкой
джобе CI. Интеграционная часть (что сокет действительно отдаёт роли то, что
здесь описано) — в test_integration_spec18_face_identity.py.
"""
import json

import pytest

from app.services.face_feed_acl import filter_face_message

FACE = {
    "type": "face",
    "event_id": 42,
    "camera_id": 7,
    "person_id": 3,
    "name": "Иванов И.И.",
    "is_known": True,
    "alert": True,
    "tags": ["watchlist"],
    "snapshot": "cam7_1754380000.jpg",
    "ts": "2026-08-23T00:00:00+00:00",
    "bbox": {"x1": 10, "y1": 20, "x2": 110, "y2": 140},
    "frame_w": 1280,
    "frame_h": 720,
}
BOX = {
    "type": "box",
    "camera_id": 7,
    "person_id": 3,
    "name": "Иванов И.И.",
    "is_known": True,
    "bbox": {"x1": 10, "y1": 20, "x2": 110, "y2": 140},
    "frame_w": 1280,
    "frame_h": 720,
}
ENHANCED = {
    "type": "enhanced",
    "event_id": 42,
    "person_id": 3,
    "snapshot": "enh_cam7_1754380000.jpg",
    "backend": "codeformer",
}


@pytest.mark.parametrize("role", ["admin", "operator"])
@pytest.mark.parametrize("payload", [FACE, BOX, ENHANCED])
def test_card_roles_get_message_byte_for_byte(role, payload):
    """Ролям, которым §18 разрешает «Карточки персон», сообщение уходит без
    пересборки.

    Проверяется именно тождество строки, а не равенство разобранных
    словарей: Стена читает поля, которых фильтр не перечисляет, и молчаливая
    потеря поля сломала бы её не на тесте, а на глазах у оператора.
    """
    raw = json.dumps(payload)
    assert filter_face_message(raw, role) is raw


@pytest.mark.parametrize("payload", [FACE, BOX])
def test_viewer_keeps_overlay_geometry(payload):
    """§4 «bounding box'ы вокруг лиц с подписями» наблюдателю разрешён —
    рамку должно быть чем нарисовать и что подписать."""
    out = json.loads(filter_face_message(json.dumps(payload), "viewer"))
    assert out["bbox"] == payload["bbox"]
    assert out["frame_w"] == payload["frame_w"]
    assert out["frame_h"] == payload["frame_h"]
    assert out["camera_id"] == payload["camera_id"]
    assert out["name"] == payload["name"]
    assert out["is_known"] is True
    assert out["type"] == payload["type"]


@pytest.mark.parametrize("payload", [FACE, BOX])
@pytest.mark.parametrize("secret", ["snapshot", "tags", "alert", "person_id", "event_id", "ts"])
def test_viewer_never_gets_card_fields(payload, secret):
    """Ядро находки: хранимый кадр, watchlist-разметка и признак персоны —
    содержимое карточки, закрытой наблюдателю строкой §18."""
    out = json.loads(filter_face_message(json.dumps(payload), "viewer"))
    assert secret not in out


def test_viewer_does_not_get_enhanced_updates():
    """`enhanced` обновляет уже показанный на Стене снимок. Стена
    наблюдателю закрыта, значит и обновление ему адресовать некуда."""
    assert filter_face_message(json.dumps(ENHANCED), "viewer") is None


def test_unknown_message_type_is_closed_by_default():
    """Умолчание закрытое: новое поле или новый тип сообщения в ленте не
    должны становиться видимыми наблюдателю сами собой, потому что автор
    изменения не вспомнил про матрицу прав."""
    future = {"type": "person_merged", "person_id": 3, "name": "Иванов И.И."}
    assert filter_face_message(json.dumps(future), "viewer") is None


def test_message_without_geometry_is_dropped_for_viewer():
    """Рамку без геометрии рисовать нечем — такое сообщение наблюдателю
    бесполезно, а значит и незачем."""
    no_geom = dict(FACE)
    no_geom.pop("bbox")
    assert filter_face_message(json.dumps(no_geom), "viewer") is None


@pytest.mark.parametrize("junk", ["не-json", "", "[1,2,3]", "null"])
def test_unparseable_message_is_not_forwarded_to_viewer(junk):
    """Нераспознанное сообщение отфильтровать нельзя, а переслать целиком
    значит отдать неизвестно что."""
    assert filter_face_message(junk, "viewer") is None


def test_unknown_role_is_treated_as_least_privileged():
    """Роль, которой нет в матрице, не должна получать карточки: список
    привилегированных ролей задан перечислением, а не исключением."""
    out = filter_face_message(json.dumps(FACE), "какая-то-новая-роль")
    assert out is not None and "snapshot" not in json.loads(out)
