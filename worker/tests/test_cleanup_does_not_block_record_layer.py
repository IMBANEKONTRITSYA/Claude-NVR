"""Долгая уборка архива не останавливает управляющий контур записи (§2, §5, §9).

**Третья находка одного класса, и первая — внутри одного слоя.** Цикл 22
закрыл «падение аналитики роняет запись», цикл 55 — «медленная загрузка
модели останавливает запись». Здесь оба конца внутри слоя записи: долгий
этап прохода менеджера задерживал соседние по тому же проходу.

`cleanup_old()` — единственный этап без предела на объём работы: у
циклической перезаписи `LIMIT 5000`, у уборки по движению `LIMIT 1000`, а
retention берёт всё просроченное и делает по два `unlink()` на сегмент. На
120 камерах суточная порция — десятки тысяч файлов по HDD. Бюджет этапа в
сторожe живости стоял ровно под это: 900 секунд, — то есть четверть часа
простоя контура записи считалась нормой.

Что именно стояло эти минуты:

* `publish_record_layer_status` — алерт §9 «потеря потока» опаздывает, а
  ключ `record:layer` в Redis (TTL 120 с) успевает протухнуть, и страница
  мониторинга показывает пустоту вместо статусов;
* `enforce_disk_quota` — циклическая перезапись. У неё в комментарии
  сказано, почему она зовётся каждые ~10 с, а не раз в час: «между
  часовыми проходами 120 камер успевают дописать ~108 ГБ». Долгая уборка
  возвращала ровно тот интервал, против которого это написано;
* `record_layer_sync` и `index_record_segments` следующего прохода.

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

# Сколько держим уборку. На объекте это минуты; здесь достаточно срока,
# заведомо большего одного прохода менеджера (10 с ожидания между
# проходами), чтобы разница между «контур ждёт уборку» и «не ждёт» была
# однозначной.
HOLD_SEC = 5.0


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
    """Один проход `manager()` со всем внешним миром на заглушках.

    Возвращает `run(cleanup)`: запускает менеджер с заданной уборкой и
    отдаёт словарь с секундами до первого вызова каждого наблюдаемого этапа
    контура записи, считая от старта менеджера.
    """
    def run(cleanup, wait_for="disk_quota", times=1, timeout=60.0):
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
        monkeypatch.setattr(worker, "enforce_disk_quota", _mark("disk_quota"))
        monkeypatch.setattr(worker, "prune_motionless_segments",
                            _mark("motion_prune"))
        monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})
        monkeypatch.setattr(worker, "cleanup_old", cleanup)
        # Пакетная кластеризация уходит в свою нить и на пустой заглушке БД
        # сыплет трассой в вывод; к проверке она отношения не имеет.
        monkeypatch.setattr(worker, "_recluster_bg", lambda: None)
        # `_try_load_model` не нужен: модель уже «загружена» (FACE_APP).
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


def test_disk_quota_runs_while_the_archive_cleanup_is_still_going(manager_pass):
    """Ключевое: циклическая перезапись идёт, не дожидаясь конца уборки.

    Обе — уборки, но у них разные задачи и разные сроки. Перезапись
    аварийна и обязана срабатывать в пределах десятка секунд: на
    переполненном томе запись встаёт целиком.

    Проверка откатом: верните вызов `cleanup_old()` в тело `manager()` — и
    задержка станет не меньше HOLD_SEC.
    """
    release = threading.Event()

    def slow_cleanup():
        release.wait(HOLD_SEC * 4)

    try:
        at = manager_pass(slow_cleanup)
    finally:
        release.set()

    assert at["disk_quota"][0] < HOLD_SEC, (
        f"циклическая перезапись ждала уборку архива {at['disk_quota'][0]:.1f} с "
        "— §2 требует, чтобы долгий этап не задерживал соседние"
    )


def test_stream_statuses_keep_being_published_during_a_long_cleanup(manager_pass):
    """Второй проход контура наступает вовремя — значит §9 не слепнет.

    Отдельно от предыдущей: там проверялся сосед по тому же проходу, здесь
    — следующий проход целиком. Именно он держит ключ `record:layer` в
    Redis живым: TTL у ключа 120 с, а уборка с прежним бюджетом занимала до
    900 с, и страница мониторинга успевала опустеть.

    Первая публикация идёт до уборки и прошла бы в любом случае — ждём
    вторую. Между проходами менеджер спит 10 с, поэтому «вовремя» здесь —
    около 10 с; уборка держится вдвое дольше, и на прежнем коде вторая
    публикация случилась бы не раньше её конца.
    """
    release = threading.Event()
    hold = 25.0

    def slow_cleanup():
        release.wait(hold)

    try:
        at = manager_pass(slow_cleanup, wait_for="record_status", times=2)
    finally:
        release.set()

    second = at["record_status"][1]
    assert second < 15.0, (
        f"вторая публикация статусов случилась через {second:.1f} с — "
        f"уборка держалась {hold:.0f} с, то есть контур записи ждал её"
    )


def test_the_cleanup_itself_still_runs(manager_pass):
    """Позитивный контроль: не ждать — не значит не убирать.

    Без него правку можно было бы «пройти», выключив уборку совсем.
    """
    done = threading.Event()

    def cleanup():
        time.sleep(0.2)
        done.set()

    manager_pass(cleanup)
    assert done.wait(30), "уборка архива так и не выполнилась"


def test_a_second_pass_is_not_started_over_a_running_one(monkeypatch):
    """Проход, не уложившийся в час, не получает дублёра.

    Два прохода выбирали бы одни и те же строки и удваивали работу — в том
    числе `unlink()` по уже удалённым файлам. Отказ виден в журнале:
    молчание здесь было бы хуже дубля.
    """
    release = threading.Event()
    monkeypatch.setattr(worker, "cleanup_old", lambda: release.wait(30))
    monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})
    monkeypatch.setattr(worker, "_cleanup_thread", None)

    try:
        assert worker.start_cleanup_pass() is True
        # Ждём, пока нить действительно начнёт работу, иначе проверка
        # «идёт ли проход» зависела бы от планировщика.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if worker._cleanup_thread.is_alive():
                break
            time.sleep(0.01)
        assert worker.start_cleanup_pass() is False, (
            "второй проход уборки запустился поверх идущего"
        )
    finally:
        release.set()
        worker._cleanup_thread.join(timeout=10)

    # После завершения предыдущего прохода следующий запускается штатно.
    monkeypatch.setattr(worker, "cleanup_old", lambda: None)
    assert worker.start_cleanup_pass() is True
    worker._cleanup_thread.join(timeout=10)
