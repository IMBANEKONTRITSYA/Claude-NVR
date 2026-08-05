"""Регрессионный тест TOCTOU-гонки дублей Person в find_or_create_person()
(P1, известный пробел с цикла 15, закрыт циклом 16 — см. docs/reviews/REVIEW_LOG.md).

До фикса SELECT ближайшего центроида → сравнение с порогом → INSERT не были
атомарны: до 16 нитей camera_worker() (по одной на камеру) делят одну БД, и
если две камеры видят одного и того же неизвестного человека в одном узком
окне, обе могли пройти SELECT до INSERT друг друга и создать два разных
Person для одного физического человека. Здесь БД не поднимается — проверяем,
что find_or_create_person() сериализует критическую секцию через
pg_advisory_xact_lock ДО SELECT ближайшего центроида (порядок важен: лок,
взятый после SELECT, гонку не закрывает), с тем же ключом при каждом вызове.

Требует полный requirements.txt воркера (см. докстринг
test_camera_worker_onvif_thread.py) — pytest.importorskip как и там."""
import os

import numpy as np
import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.), см. докстринг модуля"
)


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeRow:
    def __init__(self, id, sim):
        self.id = id
        self.sim = sim


class _FakeSession:
    """Записывает каждый execute() в порядке вызова: (sql_text, params)."""

    def __init__(self, select_row=None):
        self.calls = []
        self._select_row = select_row
        self.added = []
        self.flushed = False

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, params))
        if "pg_advisory_xact_lock" in sql:
            return _FakeResult(None)
        if "FROM persons" in sql:
            return _FakeResult(self._select_row)
        raise AssertionError(f"неожиданный SQL в тесте: {sql}")

    def add(self, obj):
        self.added.append(obj)
        obj.id = 999  # имитация PK, назначаемого при flush()

    def flush(self):
        self.flushed = True


def _emb():
    return np.zeros(512, dtype=np.float32)


def test_acquires_advisory_lock_before_select():
    s = _FakeSession(select_row=None)
    worker.find_or_create_person(s, _emb())

    assert len(s.calls) == 2, "ожидались ровно два execute(): лок, затем SELECT"
    lock_sql, lock_params = s.calls[0]
    select_sql, _ = s.calls[1]
    assert "pg_advisory_xact_lock" in lock_sql, (
        "advisory lock должен браться ДО SELECT ближайшего центроида — иначе TOCTOU-окно "
        "между чтением и записью остаётся открытым"
    )
    assert "FROM persons" in select_sql
    assert lock_params == {"key": worker.PERSON_DEDUP_LOCK_KEY}


def test_uses_stable_lock_key_across_calls():
    """Разные вызовы (в т.ч. из разных нитей/процессов) должны сериализоваться
    друг относительно друга — а значит использовать один и тот же ключ."""
    s1, s2 = _FakeSession(), _FakeSession()
    worker.find_or_create_person(s1, _emb())
    worker.find_or_create_person(s2, _emb())

    key1 = s1.calls[0][1]["key"]
    key2 = s2.calls[0][1]["key"]
    assert key1 == key2 == worker.PERSON_DEDUP_LOCK_KEY


def test_returns_existing_person_when_within_threshold():
    close_sim = 1 - (worker.CONFIG["similarity_threshold"] / 2)  # заведомо выше порога
    s = _FakeSession(select_row=_FakeRow(id=42, sim=close_sim))

    pid, matched = worker.find_or_create_person(s, _emb())

    assert (pid, matched) == (42, True)
    assert s.added == [], "при совпадении новая персона не должна создаваться"


def test_creates_new_person_when_no_match():
    s = _FakeSession(select_row=None)

    pid, matched = worker.find_or_create_person(s, _emb())

    assert matched is False
    assert pid == 999
    assert len(s.added) == 1
    assert s.flushed is True
