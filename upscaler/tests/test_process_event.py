"""Первые тесты сервиса апскейла: production path и удержание сессии БД.

До цикла 20 у `upscaler/` не было ни одного теста и ни одной CI-джобы —
единственный сервис проекта без покрытия вообще. Находка P1 этого цикла
(сессия БД, открытая на всё время инференса) сидела именно здесь, и
поймать её было нечем.

Тесты идут production path: настоящий Postgres, настоящие строки
`face_events`/`persons`, настоящие файлы на диске, настоящий
`process_event()`. Подменяется только `enhance()` — прогон GFPGAN, которого
в песочнице (и в CI) быть не может; подменяется он на функцию-наблюдателя,
то есть ровно на то, что проверяется.

`enhance()` подменяется, а не выбирается через UPSCALE_BACKEND=opencv,
намеренно: OpenCV-fallback тоже работает секунды на кадр
(`fastNlMeansDenoisingColored`), и тесты стали бы медленными, ничего не
добавив к проверяемому утверждению.
"""
import os

import pytest

# upscaler.py читает DATABASE_URL на уровне модуля через os.environ[...],
# то есть без переменной падает KeyError — а importorskip ловит только
# ImportError. Без этой проверки локальный прогон без docker-compose давал
# бы не skip, а ошибку на здоровом дереве (урок цикла 18).
if not os.environ.get("DATABASE_URL"):
    pytest.skip("нет DATABASE_URL — апскейл требует БД", allow_module_level=True)

# Тяжёлые импорты — строго после importorskip (урок цикла 16): в окружении
# без cv2/sqlalchemy джоба должна пропускаться, а не падать ImportError.
upscaler = pytest.importorskip(
    "upscaler",
    reason="нужны зависимости апскейла (cv2/numpy/sqlalchemy/redis)",
)

import numpy as np  # noqa: E402
import sqlalchemy  # noqa: E402


@pytest.fixture()
def live_db():
    """Пропуск, если Postgres недоступен (локальный прогон без
    docker-compose), — иначе здоровое дерево давало бы ложное «failed»
    (урок цикла 18)."""
    try:
        with upscaler.Session() as s:
            s.execute(sqlalchemy.text("SELECT 1"))
    except Exception as e:
        pytest.skip(f"нет живого Postgres: {e}")
    # На пустой БД (CI-джоба поднимает чистый Postgres) таблиц ещё нет:
    # их создаёт бэкенд при старте, а эта джоба его не поднимает. На
    # развёрнутой БД вызов ничего не делает — create_all не трогает
    # существующие таблицы и не добавляет недостающие колонки.
    upscaler.Base.metadata.create_all(upscaler.engine)


@pytest.fixture()
def media_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(upscaler, "MEDIA_PATH", str(tmp_path))
    (tmp_path / "snapshots").mkdir()
    return tmp_path


@pytest.fixture()
def seeded(live_db, media_dir):
    """Персона + событие + реальный файл снимка на диске.

    Возвращает (event_id, person_id, src_rel).
    """
    src_rel = "snapshots/cam99_1785000000000_abcdef12.jpg"
    import cv2

    img = np.full((60, 60, 3), 40, dtype=np.uint8)
    assert cv2.imwrite(str(media_dir / src_rel), img)

    with upscaler.Session() as s:
        person = upscaler.Person(name="test-upscaler-person", status="unknown", avatar_path=src_rel)
        s.add(person)
        s.flush()
        ev = upscaler.FaceEvent(
            camera_id=None,
            person_id=person.id,
            ts=__import__("datetime").datetime.utcnow(),
            snapshot_path=src_rel,
            orig_snapshot_path=src_rel,
            enhanced=False,
        )
        s.add(ev)
        s.commit()
        ids = (ev.id, person.id)
    try:
        yield ids[0], ids[1], src_rel
    finally:
        with upscaler.Session() as s:
            s.execute(sqlalchemy.text("DELETE FROM face_events WHERE id = :i"), {"i": ids[0]})
            s.execute(sqlalchemy.text("DELETE FROM persons WHERE id = :i"), {"i": ids[1]})
            s.commit()


def _idle_in_transaction_pids(probe_engine) -> list:
    """Бэкенды Postgres, висящие в `idle in transaction`, кроме собственного.

    Отдельный engine, а не сессия апскейла: спрашивать пул о самом себе
    изнутри его же соединения бессмысленно — нужен взгляд со стороны, как
    у мониторинга в проде.
    """
    with probe_engine.connect() as c:
        rows = c.execute(sqlalchemy.text("""
            SELECT pid FROM pg_stat_activity
            WHERE datname = current_database()
              AND pid <> pg_backend_pid()
              AND state = 'idle in transaction'
        """)).fetchall()
    return [row[0] for row in rows]


def test_no_open_transaction_while_enhancing(seeded, monkeypatch):
    """Пока крутится улучшение картинки, соединение не висит в транзакции.

    До фикса транзакция, открытая первым `s.get(FaceEvent, ...)`, жила до
    `s.commit()` — то есть весь прогон GFPGAN. Строка face_events всё это
    время заблокирована на запись, и DELETE из ротации ждёт её.
    """
    event_id, _, _ = seeded
    probe_engine = sqlalchemy.create_engine(
        upscaler.DATABASE_URL, poolclass=sqlalchemy.pool.NullPool
    )
    observed = {}

    def _observing_enhance(img):
        observed["idle_in_tx"] = _idle_in_transaction_pids(probe_engine)
        return img, "test"

    monkeypatch.setattr(upscaler, "enhance", _observing_enhance)
    try:
        upscaler.process_event(event_id)
    finally:
        probe_engine.dispose()

    assert "idle_in_tx" in observed, "enhance() не вызывался — тест ничего не проверил"
    assert observed["idle_in_tx"] == [], (
        "во время улучшения снимка открыта транзакция БД "
        f"(pid {observed['idle_in_tx']}); всё это время строка face_events "
        "заблокирована, а ротация архива ждёт"
    )


def test_row_can_be_deleted_while_enhancing(seeded, monkeypatch):
    """Ротация архива может удалить событие, пока идёт апскейл, и не ждёт.

    Прямое следствие фикса и вторая половина той же проблемы: раньше
    `DELETE FROM face_events` из `worker.cleanup_old()` блокировался на
    строке до конца инференса. Здесь DELETE идёт с отдельного соединения
    ровно в момент улучшения и с коротким lock_timeout: если фикса нет,
    он не уложится и упадёт по таймауту.
    """
    event_id, _, _ = seeded
    probe_engine = sqlalchemy.create_engine(
        upscaler.DATABASE_URL, poolclass=sqlalchemy.pool.NullPool
    )
    outcome = {}

    def _deleting_enhance(img):
        try:
            with probe_engine.connect() as c:
                c.execute(sqlalchemy.text("SET lock_timeout = '2s'"))
                c.execute(
                    sqlalchemy.text("DELETE FROM face_events WHERE id = :i"), {"i": event_id}
                )
                c.commit()
            outcome["deleted"] = True
        except Exception as e:
            outcome["deleted"] = False
            outcome["error"] = str(e)
        return img, "test"

    monkeypatch.setattr(upscaler, "enhance", _deleting_enhance)
    try:
        upscaler.process_event(event_id)
    finally:
        probe_engine.dispose()

    assert outcome.get("deleted") is True, (
        "ротация не смогла удалить событие во время апскейла: "
        f"{outcome.get('error')}"
    )


def test_enhanced_snapshot_replaces_original_and_avatar(seeded, monkeypatch):
    """Production path: событие помечено enhanced, аватар персоны обновлён.

    Не edge case — это основной и единственный сценарий сервиса. До цикла
    20 он не покрывался ничем: сервис работал только в docker-compose.
    """
    event_id, person_id, src_rel = seeded
    monkeypatch.setattr(upscaler, "enhance", lambda img: (img, "test"))

    upscaler.process_event(event_id)

    with upscaler.Session() as s:
        ev = s.get(upscaler.FaceEvent, event_id)
        person = s.get(upscaler.Person, person_id)
        assert ev.enhanced is True, "событие должно быть помечено enhanced"
        assert ev.snapshot_path == f"snapshots/enh_{os.path.basename(src_rel)}"
        assert ev.orig_snapshot_path == src_rel, "оригинал должен остаться прежним"
        assert person.avatar_path == ev.snapshot_path, "аватар персоны должен обновиться"

    assert os.path.exists(upscaler.abspath(ev.snapshot_path)), "улучшенный файл должен лежать на диске"
    assert os.path.exists(upscaler.abspath(src_rel)), "оригинал должен остаться на диске"


def test_deleted_event_leaves_no_orphan_file(seeded, monkeypatch):
    """Событие исчезло во время апскейла — улучшенный файл не остаётся сиротой."""
    event_id, _, src_rel = seeded
    enh_abs = None

    def _deleting_enhance(img):
        with upscaler.Session() as s:
            s.execute(sqlalchemy.text("DELETE FROM face_events WHERE id = :i"), {"i": event_id})
            s.commit()
        return img, "test"

    monkeypatch.setattr(upscaler, "enhance", _deleting_enhance)
    upscaler.process_event(event_id)

    enh_abs = upscaler.abspath(f"snapshots/enh_{os.path.basename(src_rel)}")
    assert not os.path.exists(enh_abs), "осиротевший улучшенный файл должен быть удалён"


def test_already_enhanced_event_is_skipped_unless_forced(seeded, monkeypatch):
    """Повторная задача на то же событие не гоняет модель ещё раз."""
    event_id, _, _ = seeded
    calls = []

    def _counting_enhance(img):
        calls.append(1)
        return img, "test"

    monkeypatch.setattr(upscaler, "enhance", _counting_enhance)

    upscaler.process_event(event_id)
    upscaler.process_event(event_id)
    assert len(calls) == 1, "второй прогон должен быть пропущен (enhanced=True)"

    upscaler.process_event(event_id, force=True)
    assert len(calls) == 2, "force=True должен принудить повтор"
