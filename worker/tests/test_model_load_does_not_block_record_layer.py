"""Медленная загрузка модели не останавливает слой записи (SPEC §2, §15).

**Вторая половина одной строки ТЗ.** §2 требует: «Слои не имеют общих
узких мест: падение/**деградация** одного не затрагивает другой».
`test_model_failure_isolation.py` рядом закрывает первое слово — падение:
отказ загрузки возвращает False, а не роняет процесс. Здесь — второе.

**Чем деградация отличалась от падения на практике.** Отказ, который
проверяет соседний набор, мгновенен: `_fail_load` бросает исключение сразу.
На объекте так ведёт себя лишь часть случаев — например, не резолвится DNS.
Файрвол с политикой DROP (для NVR это норма, а не экзотика) выглядит иначе:
пакеты пропадают, `requests.get()` внутри insightface вызван **без единого
таймаута** (`insightface/utils/download.py`), и вызов висит — на ретраях SYN
ядра порядка двух минут, а на вставшем чтении не кончается никогда.

Пока загрузка шла синхронно в нити менеджера, всё это время слой записи не
делал ничего: не синхронизировал пути MediaMTX (то есть на свежей установке
архив не писался вовсе), не индексировал сегменты, не публиковал статусы
потоков, не выполнял циклическую перезапись при заполнении диска. Сторож
живости это не ловил, а санкционировал: бюджет этапа `model_load` в
`liveness.py` составлял 900 секунд.

Проверяется поведение (успевает ли слой записи), а не форма вызова.

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

# Сколько держим загрузку. Настоящая пауза на объекте — минуты; здесь
# достаточно срока, заведомо большего одного прохода менеджера, чтобы
# разница между «слой записи ждёт загрузку» и «не ждёт» была однозначной.
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

    Возвращает функцию `run(load)`: запускает менеджер с заданной загрузкой
    модели и отдаёт время до первого вызова слоя записи.
    """
    def run(load, timeout=30.0):
        monkeypatch.setattr(worker, "FACE_APP", None)
        monkeypatch.setattr(worker, "MODEL_ERROR", None)
        monkeypatch.setattr(worker, "shutdown_event", threading.Event())
        monkeypatch.setattr(worker, "Session", _FakeSession)
        monkeypatch.setattr(worker, "refresh_config", lambda: None)
        monkeypatch.setattr(worker, "log_record_root", lambda: None)
        monkeypatch.setattr(worker, "start_watchdog", lambda hb: None)
        monkeypatch.setattr(worker, "RECORD_RECOVERY_INTERVAL", 0.0)
        monkeypatch.setattr(worker, "index_record_segments", lambda: None)
        monkeypatch.setattr(worker, "publish_record_layer_status", lambda names: {})
        monkeypatch.setattr(worker, "check_disk_alerts", lambda: None)
        monkeypatch.setattr(worker, "enforce_disk_quota", lambda: 0)
        monkeypatch.setattr(worker, "prune_motionless_segments", lambda: 0)
        monkeypatch.setattr(worker, "cleanup_old", lambda: None)
        monkeypatch.setattr(worker, "prune_orphan_media", lambda: {})
        monkeypatch.setattr(worker, "load_face_app", load)
        # embed-API поднимается настоящим uvicorn'ом на порту 9000 — в
        # тесте он не нужен и на занятом порту мешал бы.
        import embed_api
        monkeypatch.setattr(embed_api, "start_embed_api", lambda *a, **k: None)

        reached = threading.Event()
        at = {}

        def _sync(cams):
            at.setdefault("record_layer_sync", time.monotonic())
            reached.set()

        monkeypatch.setattr(worker, "record_layer_sync", _sync)

        started = time.monotonic()
        t = threading.Thread(target=worker.manager, daemon=True)
        t.start()
        ok = reached.wait(timeout)
        worker.shutdown_event.set()
        t.join(timeout=15)
        assert ok, "слой записи не отработал ни разу"
        return at["record_layer_sync"] - started

    return run


def test_record_layer_runs_while_the_model_is_still_loading(manager_pass):
    """Ключевое: синхронизация путей MediaMTX идёт, не дожидаясь модели.

    Проверка откатом: верните синхронный `_try_load_model()` в manager() —
    и задержка станет не меньше HOLD_SEC.
    """
    release = threading.Event()

    def slow_load(*a, **k):
        release.wait(HOLD_SEC * 4)
        return object()

    try:
        delay = manager_pass(slow_load)
    finally:
        release.set()

    assert delay < HOLD_SEC, (
        f"слой записи ждал загрузку модели {delay:.1f} с — §2 требует, "
        "чтобы деградация аналитики его не затрагивала"
    )


def test_a_load_that_never_returns_does_not_stop_the_record_layer(manager_pass):
    """Худший случай — вставшее чтение без таймаута — тоже не блокирует.

    Именно он, а не мгновенный отказ, и не кончается сам: у
    `requests.get()` в insightface нет ни connect-, ни read-таймаута.
    """
    forever = threading.Event()

    def hanging_load(*a, **k):
        forever.wait(120)
        return object()

    try:
        delay = manager_pass(hanging_load)
    finally:
        forever.set()

    assert delay < HOLD_SEC


def test_analytics_still_comes_up_after_a_slow_load(manager_pass):
    """Позитивный контроль: не ждать — не значит потерять аналитику.

    Без него правку можно было бы «пройти», выключив загрузку совсем.
    """
    loaded = threading.Event()

    def slow_load(*a, **k):
        # Задержка заведомо больше одного прохода менеджера, но конечная:
        # модель обязана доехать сама, без перезапуска процесса.
        time.sleep(1.0)
        loaded.set()
        return object()

    delay = manager_pass(slow_load)
    assert delay < HOLD_SEC
    assert loaded.wait(30), "модель так и не загрузилась"


def test_slow_load_is_visible_in_the_monitoring_payload(monkeypatch):
    """§9 обязан отличать «модель грузится» от «модель не загрузилась».

    С этого цикла первое состояние стало обычным и длительным: запись уже
    идёт, аналитика ещё поднимается. Раньше оба случая выглядели на
    странице одинаково — `model_ready: false` без пояснений.
    """
    from model_loader import LOADING

    release = threading.Event()
    loader = worker.ModelLoader(lambda p: release.wait(30), poll_sec=0.01)
    loader.start()
    monkeypatch.setattr(worker, "_model_loader", loader)
    monkeypatch.setattr(worker, "FACE_APP", None)

    class _Client:
        def __init__(self, *a, **k):
            pass

        def runtime_paths(self):
            return {}

    monkeypatch.setattr(worker, "MediaMTXClient", _Client)
    monkeypatch.setattr(worker, "_last_segments", lambda ids: {})
    monkeypatch.setattr(worker.r, "set", lambda *a, **k: True)

    try:
        loader.request(("buffalo_s", 640, 1))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if loader.snapshot()["state"] == LOADING:
                break
            time.sleep(0.01)

        payload = worker.publish_record_layer_status([])
        assert payload["analytics"]["model_ready"] is False
        assert payload["analytics"]["load"]["state"] == LOADING
        assert payload["analytics"]["load"]["model"] == "buffalo_s"
    finally:
        release.set()
        loader.stop()
