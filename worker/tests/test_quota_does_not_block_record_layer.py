"""Долгая циклическая перезапись не останавливает контур записи (§2, §5, §9).

**Четвёртая находка одного класса.** Цикл 22 закрыл «падение аналитики
роняет запись», цикл 55 — «медленная загрузка модели останавливает
запись», цикл 57 — «долгая уборка архива останавливает соседей по
проходу». Здесь — последний этап того же прохода, переживший обе правки:
строка `"disk_quota": 900.0` осталась в таблице бюджетов сторожа живости
страницей ниже объяснения, почему такой строки там быть не должно.

**Почему у перезаписи этот довод весит больше, а не меньше.**

* **Частота.** Уборка идёт раз в час; перезапись зовётся **каждым проходом
  менеджера (~10 с)**, и ровно потому, что «между часовыми проходами 120
  камер успевают дописать ~108 ГБ» (комментарий при вызове). Пока том
  переполнен, долгий проход не эпизод, а установившийся режим: следующий
  стартует сразу за предыдущим.
* **Момент.** Перезапись срабатывает тогда и только тогда, когда места уже
  нет, — то есть когда статусы потоков и алерт §9 «переполнение диска»
  нужнее всего.
* **Предел.** `LIMIT 5000` ограничивает строки, а не время: это до 10 000
  `unlink()` (файл плюс миниатюра) по HDD-массиву. Бюджет 900 с и был
  выставлен под это.

Что именно стояло эти минуты: `record_layer_sync` и
`index_record_segments` следующего прохода и `publish_record_layer_status`
— то есть ключ `record:layer` в Redis (TTL 120 с) успевал протухнуть, и
страница мониторинга показывала пустоту вместо статусов.

Проверяется поведение (успевает ли контур записи), а не форма вызова.

Требует полный requirements.txt воркера (урок цикла 16).
"""
import os
import threading
import time

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.)"
)

# Пауза менеджера между проходами (`shutdown_event.wait(10)`). Она и есть
# пол этих проверок: быстрее контур записи не оборачивается ни при какой
# правке.
MANAGER_PASS_SEC = 10.0


class _FakeSession:
    """Пустой список камер: проверяется порядок работы менеджера, а не
    содержимое БД."""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return self

    def scalars(self):
        return self

    def all(self):
        return []


@pytest.fixture
def manager_pass(monkeypatch):
    """Проходы `manager()` со всем внешним миром на заглушках.

    Возвращает `run(quota, ...)`: запускает менеджер с заданной перезаписью
    и отдаёт словарь с секундами до каждого вызова наблюдаемых этапов
    контура записи, считая от старта менеджера.
    """
    def run(quota, wait_for="record_status", times=2, timeout=90.0):
        started = time.monotonic()
        at: dict[str, list[float]] = {}
        reached = threading.Event()

        def _mark(name):
            def _fn(*a, **k):
                at.setdefault(name, []).append(time.monotonic() - started)
                if name == wait_for and len(at[name]) >= times:
                    reached.set()
                return {} if name == "record_status" else 0
            return _fn

        monkeypatch.setattr(worker, "FACE_APP", object())
        monkeypatch.setattr(worker, "MODEL_ERROR", None)
        monkeypatch.setattr(worker, "shutdown_event", threading.Event())
        monkeypatch.setattr(worker, "Session", _FakeSession)
        monkeypatch.setattr(worker, "refresh_config", lambda: None)
        monkeypatch.setattr(worker, "log_record_root", lambda: None)
        monkeypatch.setattr(worker, "start_watchdog", lambda hb: None)
        monkeypatch.setattr(worker, "RECORD_RECOVERY_INTERVAL", 0.0)
        monkeypatch.setattr(worker, "record_layer_sync", _mark("record_sync"))
        monkeypatch.setattr(worker, "index_record_segments", _mark("index"))
        monkeypatch.setattr(worker, "publish_record_layer_status",
                            _mark("record_status"))
        monkeypatch.setattr(worker, "check_disk_alerts", _mark("disk_alerts"))
        monkeypatch.setattr(worker, "prune_motionless_segments",
                            _mark("motion_prune"))
        monkeypatch.setattr(worker, "enforce_disk_quota", quota)
        # Уборка архива к этим проверкам отношения не имеет и заглушена
        # целиком: иначе её нить стартовала бы на первом же проходе и
        # добавляла шум в измеряемые промежутки.
        monkeypatch.setattr(worker, "start_cleanup_pass", lambda: True)
        monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})
        # Пакетная кластеризация уходит в свою нить и на пустой заглушке БД
        # сыплет трассой в вывод; к проверке она отношения не имеет.
        monkeypatch.setattr(worker, "_recluster_bg", lambda: None)
        monkeypatch.setattr(worker, "_quota_thread", None)
        import embed_api
        monkeypatch.setattr(embed_api, "start_embed_api", lambda *a, **k: None)

        t = threading.Thread(target=worker.manager, daemon=True)
        t.start()
        ok = reached.wait(timeout)
        worker.shutdown_event.set()
        t.join(timeout=15)
        assert ok, f"этап {wait_for} не отработал {times} раз(а)"
        return at

    return run


def test_stream_statuses_keep_being_published_during_a_long_quota_pass(manager_pass):
    """Главное: следующий проход контура наступает вовремя.

    Перезапись стоит в проходе менеджера ПОСЛЕДНЕЙ, поэтому её длительность
    добавлялась целиком к паузе перед всем следующим проходом. Именно он
    держит ключ `record:layer` в Redis живым: TTL у ключа 120 с, а бюджет
    этапа стоял 900 с — страница мониторинга успевала опустеть.

    Первая публикация идёт до перезаписи и прошла бы в любом случае — ждём
    вторую. Между проходами менеджер спит 10 с, поэтому «вовремя» здесь —
    около 10 с; перезапись держится вдвое дольше, и на прежнем коде вторая
    публикация случилась бы не раньше её конца.

    Проверка откатом: верните вызов `enforce_disk_quota()` в тело
    `manager()` — и промежуток станет не меньше HOLD.
    """
    release = threading.Event()
    hold = 25.0
    held = threading.Event()

    def slow_quota():
        # Держит только первый проход: держи он каждый, разницы между «до» и
        # «после» не было бы видно ни в одном варианте.
        if held.is_set():
            return 0
        held.set()
        release.wait(hold)
        return 0

    try:
        at = manager_pass(slow_quota)
    finally:
        release.set()

    interval = at["record_status"][1] - at["record_status"][0]
    assert interval < MANAGER_PASS_SEC + 5.0, (
        f"вторая публикация статусов случилась через {interval:.1f} с после "
        f"первой — перезапись держалась {hold:.0f} с, то есть контур записи "
        "ждал её"
    )


def test_the_next_sync_and_indexing_also_come_on_time(manager_pass):
    """Не только статусы: пути в MediaMTX и индексация сегментов — тоже.

    Отдельно от предыдущей, потому что задета вся голова следующего прохода,
    а не одна его строка: пока перезапись держала нить менеджера, новая
    камера не появлялась в MediaMTX и дописанные сегменты не попадали в
    архив всё это время.
    """
    release = threading.Event()
    hold = 25.0
    held = threading.Event()

    def slow_quota():
        if held.is_set():
            return 0
        held.set()
        release.wait(hold)
        return 0

    try:
        at = manager_pass(slow_quota, wait_for="index", times=2)
    finally:
        release.set()

    for stage in ("record_sync", "index"):
        interval = at[stage][1] - at[stage][0]
        assert interval < MANAGER_PASS_SEC + 5.0, (
            f"этап {stage} повторился через {interval:.1f} с — контур записи "
            f"ждал перезапись, которая держалась {hold:.0f} с"
        )


def test_the_quota_pass_itself_still_runs(manager_pass):
    """Позитивный контроль: не ждать — не значит не перезаписывать.

    Без него правку можно было бы «пройти», выключив перезапись совсем, — а
    выключенная перезапись означает, что на переполненном томе запись
    встанет.
    """
    done = threading.Event()

    def quota():
        time.sleep(0.2)
        done.set()
        return 0

    manager_pass(quota, wait_for="record_status", times=2)
    assert done.wait(30), "циклическая перезапись так и не выполнилась"


def test_a_second_pass_is_not_started_over_a_running_one(monkeypatch):
    """Идущий проход не получает дублёра.

    Здесь это не редкость, а норма: проход запрашивается каждые ~10 с, и
    любой, кто идёт дольше, встретит следующий запрос. Два прохода выбирали
    бы одни и те же старейшие строки и удваивали работу.

    В отличие от уборки, предупреждение в журнал при этом НЕ пишется: раз в
    десять секунд оно превратило бы журнал переполненного тома в поток.
    """
    release = threading.Event()
    monkeypatch.setattr(worker, "enforce_disk_quota", lambda: release.wait(30))
    monkeypatch.setattr(worker, "_quota_thread", None)

    try:
        assert worker.start_quota_pass() is True
        # Ждём, пока нить действительно начнёт работу, иначе проверка
        # «идёт ли проход» зависела бы от планировщика.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if worker._quota_thread.is_alive():
                break
            time.sleep(0.01)
        assert worker.start_quota_pass() is False, (
            "второй проход перезаписи запустился поверх идущего"
        )
    finally:
        release.set()
        worker._quota_thread.join(timeout=10)

    # После завершения предыдущего прохода следующий запускается штатно.
    monkeypatch.setattr(worker, "enforce_disk_quota", lambda: 0)
    assert worker.start_quota_pass() is True
    worker._quota_thread.join(timeout=10)


def test_disk_quota_is_not_watched_by_the_liveness_watchdog():
    """Этап убран из таблицы бюджетов — иначе сторож убил бы воркер.

    Реакция сторожа — `os._exit()`, то есть перезапуск процесса вместе со
    слоем записи. Убивать воркер из-за медленного тома значит нарушать §2
    самим лечением — тот же довод, по которому оттуда ушли `model_load`
    (цикл 55) и `cleanup` (цикл 57).

    Проверка откатом: верните строку `"disk_quota": 900.0` в
    `STAGE_BUDGETS_SEC` — тест упадёт.
    """
    from liveness import STAGE_BUDGETS_SEC
    assert "disk_quota" not in STAGE_BUDGETS_SEC
