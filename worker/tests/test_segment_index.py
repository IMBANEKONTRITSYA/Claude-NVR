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

**И ровно поэтому у неё обязан быть внешний ключ на `cameras`** (цикл 53).
До этого цикла таблицы `cameras` здесь не было вовсе, а `camera_id` был
простым `Integer` — и набор из семи тестов, объявленный «production path
целиком», не мог заметить, что удаление камеры останавливает индексацию
архива навсегда: в бою `camera_id` объявлен
`ForeignKey("cameras.id", ondelete="CASCADE")`, вставка строки на
несуществующую камеру отвергается, а идёт она одной транзакцией на весь
проход. Модель, упрощённая относительно боевой, проверяет упрощённую
систему; признак для carryover — тот же, что у теста, сочиняющего формат
чужой программы (цикл 52).

SQLite внешние ключи по умолчанию **не проверяет**, поэтому ниже стоит
`PRAGMA foreign_keys=ON`: без него лёгкая джоба воркера снова осталась бы
слепа к этому классу, а он и обнаружился только на Postgres.
"""
import os
import time
from datetime import datetime

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy", reason="нужен SQLAlchemy")

from sqlalchemy import (BigInteger, Column, DateTime, ForeignKey,  # noqa: E402
                        Integer, String, create_engine, event, select)
from sqlalchemy.exc import IntegrityError  # noqa: E402
from sqlalchemy.orm import declarative_base, sessionmaker  # noqa: E402

from segment_index import CONTINUOUS, index_new_segments  # noqa: E402

Base = declarative_base()


class Camera(Base):
    __tablename__ = "cameras_index_test"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), default="")


class VideoSegment(Base):
    __tablename__ = "video_segments_index_test"
    id = Column(Integer, primary_key=True)
    # Как в бою (backend/app/models.py): внешний ключ с каскадом. Каскад
    # здесь не декорация — он и создаёт состояние «файл есть, строки нет»,
    # ради которого написан test_segments_of_a_deleted_camera_*.
    camera_id = Column(Integer, ForeignKey("cameras_index_test.id", ondelete="CASCADE"),
                       index=True)
    started_at = Column(DateTime)
    ended_at = Column(DateTime)
    file_path = Column(String(500))
    event_type = Column(String(20))
    duration_sec = Column(Integer)
    # SPEC §21: размер сегмента фиксируется при индексации — по нему
    # считается фактический расход диска и выбираются жертвы циклической
    # перезаписи. Колонка обязана быть и здесь: модель передаётся в
    # index_new_segments() параметром, и её расхождение с моделью воркера —
    # ровно тот класс ошибки, который этот тест должен ловить.
    size_bytes = Column(BigInteger, default=0)


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
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _rec):  # pragma: no cover - тривиальный хук
            dbapi_conn.execute("PRAGMA foreign_keys=ON")

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


def _cameras(session_factory, *ids):
    """Заводит камеры: без строки в `cameras` сегмент в архив не попадёт."""
    with session_factory() as s:
        for cam_id in ids:
            s.add(Camera(id=cam_id, name=f"cam{cam_id}"))
        s.commit()


def _drop_camera(session_factory, cam_id):
    """Удаление камеры через веб-интерфейс (SPEC §3): строки архива уносит
    каскад, файлы на диске остаются."""
    with session_factory() as s:
        s.delete(s.get(Camera, cam_id))
        s.commit()


def _index(session_factory, d, **kwargs):
    kwargs.setdefault("now", time.time())
    kwargs.setdefault("from_timestamp", datetime.utcfromtimestamp)
    return index_new_segments(session_factory, VideoSegment, str(d),
                              camera_model=Camera, **kwargs)


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
    _cameras(session_factory, 3)
    _write(d / "cam3_1754460000.mp4")
    _write(d / "cam3_1754460300.mp4")

    added = _index(session_factory, d)

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
    _cameras(session_factory, 1)
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")

    now = time.time()
    first = _index(session_factory, d, now=now)
    second = _index(session_factory, d, now=now)

    assert (first, second) == (2, 0)
    assert len(_rows(session_factory)) == 2


def test_segment_size_is_recorded_for_storage_forecast(tmp_path, session_factory):
    """Размер файла доезжает в архив (SPEC §21).

    Это единственный источник фактического расхода диска: прогноз «на
    сколько дней хватит места» и выбор жертв циклической перезаписи
    считаются по этой колонке. Ноль здесь означал бы, что прогноз молча
    откатывается на номинал и никогда не калибруется.
    """
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 7)
    _write(d / "cam7_1000.mp4", size=1_048_576)

    _index(session_factory, d)

    assert [r.size_bytes for r in _rows(session_factory)] == [1_048_576]


def test_segment_being_written_is_not_indexed_yet(tmp_path, session_factory):
    """Строка архива на растущий файл показала бы пользователю запись,
    которая ещё не дописана, — и с неверной длительностью."""
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 1)
    _write(d / "cam1_1000.mp4", age_sec=1.0)

    added = _index(session_factory, d, settle_sec=30)

    assert added == 0 and _rows(session_factory) == []


def test_empty_segment_is_not_indexed(tmp_path, session_factory):
    """MediaMTX создаёт файл сразу при появлении потока и закрывает его
    пустым, если камера отвалилась в ту же секунду. Строка архива на такой
    файл выглядит как доступная запись и отдаёт 404 при скачивании."""
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 1)
    _write(d / "cam1_1000.mp4", size=0)
    _write(d / "cam1_1300.mp4", size=8192)

    _index(session_factory, d)

    assert [os.path.basename(r.file_path) for r in _rows(session_factory)] == ["cam1_1300.mp4"]


def test_new_segments_are_added_next_to_existing_ones(tmp_path, session_factory):
    """Второй проход после появления новых файлов должен добавить только их."""
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 1)
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")
    _index(session_factory, d)

    _write(d / "cam1_1600.mp4")
    added = _index(session_factory, d)

    assert added == 1
    assert len(_rows(session_factory)) == 3


# --- Удалённая камера (цикл 53) -------------------------------------------
#
# Состояние возникает при обычной операции §3 «удаление IP-камер через
# веб-интерфейс»: строки архива уносит `ON DELETE CASCADE`, файлы остаются
# на диске, а имя файла продолжает называть камеру, которой уже нет.


def test_segments_of_a_deleted_camera_are_skipped(tmp_path, session_factory):
    """Сегменты удалённой камеры не заносятся — их камеры нет в `cameras`."""
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 1)
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")
    _index(session_factory, d)
    assert len(_rows(session_factory)) == 2

    _drop_camera(session_factory, 1)
    assert _rows(session_factory) == []          # каскад унёс строки
    assert len(os.listdir(d)) == 2               # файлы остались

    assert _index(session_factory, d) == 0
    assert _rows(session_factory) == []


def test_one_deleted_camera_does_not_stop_indexing_the_others(tmp_path, session_factory):
    """Главное следствие: вставка идёт одной транзакцией на весь проход.

    До цикла 53 отказ внешнего ключа на файлах удалённой камеры откатывал
    её целиком, вместе с сегментами всех **живых** камер. Файлы-сироты со
    временем не исчезают (ни retention, ни перезаписи нечего выбирать —
    строк нет), поэтому отказ повторялся каждые ~10 с бесконечно: одно
    удаление камеры останавливало индексацию архива навсегда, а в журнале
    оставалась одна строка ошибки.
    """
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 1, 2)
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")
    _write(d / "cam2_1000.mp4")
    _write(d / "cam2_1300.mp4")
    _index(session_factory, d)

    _drop_camera(session_factory, 1)
    # Слой записи живой камеры продолжает работать.
    _write(d / "cam2_1600.mp4")

    added = _index(session_factory, d)

    assert added == 1, "новый сегмент живой камеры обязан попасть в архив"
    assert {r.camera_id for r in _rows(session_factory)} == {2}
    assert len(_rows(session_factory)) == 3


def test_the_foreign_key_is_really_enforced_here(tmp_path, session_factory):
    """Сторож самого набора: без внешнего ключа два теста выше зелены и на
    сломанном коде.

    Проверяется не поведение системы, а то, что схема этого набора
    воспроизводит боевую. На SQLite без `PRAGMA foreign_keys=ON` вставка
    ниже проходит молча — и лёгкая джоба воркера снова оказалась бы слепа.
    """
    with session_factory() as s:
        s.add(VideoSegment(camera_id=999, started_at=datetime.utcnow(),
                           ended_at=datetime.utcnow(), file_path="/x.mp4",
                           event_type=CONTINUOUS, duration_sec=1, size_bytes=1))
        with pytest.raises(IntegrityError):
            s.commit()


def test_the_boundary_never_hides_a_new_segment(tmp_path, session_factory):
    """Отсечка по «докуда архив заполнен» (цикл 53) — оптимизация, и её
    единственный способ навредить в том, чтобы пропустить настоящую
    запись. Проверяется на трёх последовательных проходах: архив обязан
    догонять диск на каждом.
    """
    d = tmp_path / "segments"
    d.mkdir()
    _cameras(session_factory, 1, 2)
    _write(d / "cam1_1000.mp4")
    _write(d / "cam1_1300.mp4")
    assert _index(session_factory, d) == 2

    # Новый сегмент той же камеры и первый сегмент второй камеры, у
    # которой в архиве нет ничего (её отсечка отсутствует вовсе).
    _write(d / "cam1_1600.mp4")
    _write(d / "cam2_1000.mp4")
    _write(d / "cam2_1300.mp4")
    assert _index(session_factory, d) == 3

    _write(d / "cam1_1900.mp4")
    assert _index(session_factory, d) == 1

    rows = _rows(session_factory)
    assert sorted((r.camera_id, os.path.basename(r.file_path)) for r in rows) == [
        (1, "cam1_1000.mp4"), (1, "cam1_1300.mp4"), (1, "cam1_1600.mp4"),
        (1, "cam1_1900.mp4"), (2, "cam2_1000.mp4"), (2, "cam2_1300.mp4"),
    ]
