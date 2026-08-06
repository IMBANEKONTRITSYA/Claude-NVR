"""Индексация сегментов слоя записи в `video_segments` (цикл 24).

Покрывается **production path целиком**, а не только пограничные случаи:
настоящие файлы на диске → настоящая БД через SQLAlchemy → строки архива,
которые дальше читает `/api/archive/segments`. Мокается только время.

По умолчанию БД — файловый SQLite: проверка обязана выполняться и в
CI-джобе воркера, где нет сервиса Postgres, а не превращаться там в пропуск.
Настоящий Postgres подключается отдельной переменной
`SEGMENT_INDEX_TEST_DATABASE_URL` — намеренно не `DATABASE_URL`: половина
тестов воркера выставляет её в заведомо нерабочую заглушку
(`postgresql://test:test@localhost/test`) на уровне модуля, только чтобы
импортировался `worker.py`, и её значение при общем прогоне зависит от
порядка сбора тестов. Проверено в цикле 24 против настоящего Postgres 16:
`SEGMENT_INDEX_TEST_DATABASE_URL=postgresql://... pytest tests/test_segment_index.py`.

Модель здесь объявляется своя, минимальная: в `worker.py` `VideoSegment`
соседствует с моделями на `pgvector`, и импорт оттуда потащил бы
cv2/insightface.
"""
import os
import time
from datetime import datetime

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy", reason="нужен SQLAlchemy")

from sqlalchemy import Column, DateTime, Integer, String, create_engine, select  # noqa: E402
from sqlalchemy.orm import declarative_base, sessionmaker  # noqa: E402

from segment_index import CONTINUOUS, index_new_segments  # noqa: E402

Base = declarative_base()


class VideoSegment(Base):
    __tablename__ = "video_segments_index_test"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, index=True)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    file_path = Column(String(500))
    event_type = Column(String(20))
    duration_sec = Column(Integer)


@pytest.fixture()
def session_factory(tmp_path):
    url = os.environ.get("SEGMENT_INDEX_TEST_DATABASE_URL")
    if url:
        # Воркер и апскейл ходят синхронным psycopg2; бэкенд задаёт
        # асинхронный DSN — приводим к синхронному, как это делает
        # backend/tests/conftest.py.
        url = url.replace("+asyncpg", "")
    else:
        url = f"sqlite:///{tmp_path / 'archive.db'}"
    engine = create_engine(url)
    # Своя таблица с уникальным именем: тесты воркера могут идти по той же
    # БД, что и тесты бэкенда, и не должны трогать его схему (урок цикла 23,
    # PR #68).
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine)
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def _write(path, size=4096, age_sec=120.0):
    with open(path, "wb") as fh:
        fh.write(b"\0" * size)
    when = time.time() - age_sec
    os.utime(path, (when, when))
    return path


def _rows(session_factory):
    with session_factory() as s:
        return s.execute(
            select(VideoSegment).order_by(VideoSegment.started_at)
        ).scalars().all()


def test_finished_segments_become_archive_rows(tmp_path, session_factory):
    """Полный production path: MediaMTX дописал два сегмента — оба обязаны
    оказаться в архиве с правильными границами и длительностью."""
    d = tmp_path / "segments"
    d.mkdir()
    _write(d / "cam3_1754460000.mp4")
    _write(d / "cam3_1754460300.mp4")

    added = index_new_segments(
        session_factory, VideoSegment, str(d),
        now=time.time(), from_timestamp=datetime.utcfromtimestamp,
    )

    assert added == 2
    rows = _rows(session_factory)
    assert [r.camera_id for r in rows] == [3, 3]
    first = rows[0]
    assert first.file_path == str(d / "cam3_1754460000.mp4")
    assert first.started_at == datetime.utcfromtimestamp(1754460000)
    assert first.ended_at == datetime.utcfromtimestamp(1754460300)
    assert first.duration_sec == 300
    assert first.event_type == CONTINUOUS


def test_repeated_indexing_does_not_duplicate_rows(tmp_path, session_factory):
    """Индексация стоит в цикле менеджера и выполняется каждые ~10 с. Без
    идемпотентности архив за сутки распух бы в 8640 раз."""
    d = tmp_path / "segments"
    d.mkdir()
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")

    kwargs = dict(now=time.time(), from_timestamp=datetime.utcfromtimestamp)
    first = index_new_segments(session_factory, VideoSegment, str(d), **kwargs)
    second = index_new_segments(session_factory, VideoSegment, str(d), **kwargs)

    assert (first, second) == (2, 0)
    assert len(_rows(session_factory)) == 2


def test_segment_being_written_is_not_indexed_yet(tmp_path, session_factory):
    """Строка архива на растущий файл показала бы пользователю запись,
    которая ещё не дописана, — и с неверной длительностью."""
    d = tmp_path / "segments"
    d.mkdir()
    _write(d / "cam1_1000.mp4", age_sec=1.0)

    added = index_new_segments(
        session_factory, VideoSegment, str(d),
        now=time.time(), from_timestamp=datetime.utcfromtimestamp, settle_sec=30,
    )

    assert added == 0 and _rows(session_factory) == []


def test_empty_segment_is_not_indexed(tmp_path, session_factory):
    """MediaMTX создаёт файл сразу при появлении потока и закрывает его
    пустым, если камера отвалилась в ту же секунду. Строка архива на такой
    файл выглядит как доступная запись и отдаёт 404 при скачивании."""
    d = tmp_path / "segments"
    d.mkdir()
    _write(d / "cam1_1000.mp4", size=0)
    _write(d / "cam1_1300.mp4", size=8192)

    index_new_segments(
        session_factory, VideoSegment, str(d),
        now=time.time(), from_timestamp=datetime.utcfromtimestamp,
    )

    assert [os.path.basename(r.file_path) for r in _rows(session_factory)] == ["cam1_1300.mp4"]


def test_new_segments_are_added_next_to_existing_ones(tmp_path, session_factory):
    """Второй проход после появления новых файлов должен добавить только их."""
    d = tmp_path / "segments"
    d.mkdir()
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")
    kwargs = dict(from_timestamp=datetime.utcfromtimestamp)
    index_new_segments(session_factory, VideoSegment, str(d), now=time.time(), **kwargs)

    _write(d / "cam1_1600.mp4")
    added = index_new_segments(session_factory, VideoSegment, str(d), now=time.time(), **kwargs)

    assert added == 1
    assert len(_rows(session_factory)) == 3
