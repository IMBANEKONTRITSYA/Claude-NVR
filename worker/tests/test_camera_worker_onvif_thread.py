"""Регрессионный тест утечки нитей onvif_poll_worker (P1, цикл 15).

До фикса нить ONVIF-поллинга запускалась безусловно при входе в
camera_worker() — до проверки, что RTSP-поток аналитики вообще открылся.
onvif_poll_worker — daemon-нить без собственного условия остановки, кроме
глобального shutdown_event (см. её докстринг в worker.py), поэтому она
переживала любой ранний return camera_worker(). Для камеры с валидным
onvif_host, но постоянно неоткрывающимся RTSP-субпотоком (частая реальная
поломка: неверный URL/пароль именно субпотока при рабочем основном потоке)
manager() пересоздаёт camera_worker() каждые ~10с — и каждый перезапуск
плодил ещё одну независимую ONVIF-нить со своей PullPoint-подпиской и
сокетом, ни одна из которых никогда не останавливалась: неограниченная
утечка нитей, в итоге валящая процесс воркера целиком (все камеры, не
только сбойную).

Требует полный requirements.txt воркера (cv2/numpy/redis/cryptography/
sklearn/sqlalchemy/pgvector) для самого импорта worker.py — CI worker-джоб
намеренно ставит только лёгкие зависимости (см. .github/workflows/ci.yml,
opencv/insightface/onnxruntime слишком тяжелы для обычного CI-прогона),
поэтому здесь используется pytest.importorskip: там, где полных
зависимостей нет, тест аккуратно пропускается при сборе, а не падает
ошибкой импорта. Проверяется вручную в циклах аудита с полным
requirements.txt (см. docs/reviews/REVIEW_LOG.md)."""
import os
import threading

import pytest

os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

worker = pytest.importorskip(
    "worker", reason="требует полный requirements.txt воркера (cv2 и т.д.), см. докстринг модуля"
)

ONVIF_CONFIG = {"host": "192.168.1.64", "port": 80, "username": "admin", "password": "s3cret"}


class _NeverOpensCapture:
    """Имитирует постоянно недоступный RTSP-поток аналитики (например,
    неверный URL/пароль субпотока)."""

    def isOpened(self):
        return False

    def release(self):
        pass


class _OpensThenStallsCapture:
    """Открывается успешно, но сразу же перестаёт отдавать кадры — камера
    online, но проверяемый путь (запуск ONVIF-нити) должен сработать
    именно на этом, успешном открытии."""

    def isOpened(self):
        return True

    def get(self, prop_id):
        return 25.0

    def read(self):
        return False, None

    def release(self):
        pass


def test_camera_worker_does_not_start_onvif_thread_when_capture_never_opens(monkeypatch):
    started = []
    monkeypatch.setattr(worker, "open_capture", lambda url: _NeverOpensCapture())
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "onvif_poll_worker", lambda *a, **kw: started.append(a))

    # resolve_snapshot_url() замокан: без этого camera_worker() на каждом из
    # пяти проходов делает РЕАЛЬНЫЙ ONVIF-вызов get_profiles() к host из
    # ONVIF_CONFIG (192.168.1.64) с таймаутом 5 с. На машине автора адрес
    # недоступен мгновенно, на CI-раннере connect к нему висит до таймаута —
    # пять раз, до 25 с на пустом месте. Тест проверяет запуск нити, а не
    # разрешение снимка, поэтому сеть здесь лишняя.
    monkeypatch.setattr(worker, "resolve_snapshot_url", lambda *a, **kw: None)

    # Имитируем manager(), пересоздающий camera_worker() на каждом тике,
    # пока RTSP не откроется (в этом тесте — никогда).
    for _ in range(5):
        worker.camera_worker(1, "rtsp://cam/main", face_app=None,
                              sub_rtsp_url="rtsp://cam/sub", onvif_config=ONVIF_CONFIG)

    assert started == [], (
        "onvif_poll_worker не должен запускаться, пока RTSP-поток аналитики ни разу "
        "не открылся — иначе каждый перезапуск camera_worker() менеджером плодит "
        "независимую, никогда не останавливающуюся нить (утечка нитей)"
    )


def test_camera_worker_starts_onvif_thread_after_capture_opens(monkeypatch):
    started = threading.Event()
    monkeypatch.setattr(worker, "open_capture", lambda url: _OpensThenStallsCapture())
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "load_cam_state", lambda cam_id: (None, True, None, None))

    # resolve_snapshot_url() теперь резолвится в отдельной фоновой нити после
    # открытия захвата, а не первым синхронным действием camera_worker() (см.
    # snapshot_url_holder в worker.py). Мок оставлен, чтобы фоновая нить не
    # ходила в реальную сеть к недостижимому 192.168.1.64: тест проверяет
    # запуск ONVIF-нити, а не разрешение снимка. Что медленный resolve больше
    # НЕ сдвигает старт нити — отдельный тест ниже
    # (test_slow_snapshot_resolve_does_not_block_worker_startup).
    monkeypatch.setattr(worker, "resolve_snapshot_url", lambda *a, **kw: None)

    # Свой, гарантированно снятый shutdown_event на время теста, а не общий
    # модульный: тест запускает camera_worker() в нити и должен её же
    # остановить, ни на что не влияя. Общий объект мутируют и соседние наборы
    # воркера (manager()-тесты, mode_gating, resource_release) — изолированный
    # Event исключает и чтение чужого состояния, и задевание чужих нитей своим
    # set() в finally. Тот же приём, что в *_does_not_block_record_layer.
    monkeypatch.setattr(worker, "shutdown_event", threading.Event())

    def fake_onvif_poll_worker(*args, **kwargs):
        started.set()
        while not worker.shutdown_event.is_set():
            worker.shutdown_event.wait(0.05)

    monkeypatch.setattr(worker, "onvif_poll_worker", fake_onvif_poll_worker)

    # cap.read() возвращает (False, None) сразу — camera_worker() уходит в
    # ветку reconnect и ждёт там shutdown_event, не блокируя тест надолго.
    t = threading.Thread(
        target=worker.camera_worker,
        args=(1, "rtsp://cam/main", None),
        kwargs={"sub_rtsp_url": "rtsp://cam/sub", "onvif_config": ONVIF_CONFIG},
        daemon=True,
    )
    t.start()
    try:
        # Запас по времени против планировщика нагруженного CI-раннера: тест
        # проверяет сам факт старта нити, а не её скорость, поэтому дешевле
        # подождать дольше, чем ловить редкое ложное падение.
        assert started.wait(timeout=15), "onvif_poll_worker должен стартовать после успешного открытия потока"
    finally:
        worker.shutdown_event.set()
        t.join(timeout=10)
        assert not t.is_alive(), "camera_worker() не остановился по shutdown — нить утекла бы в соседние тесты"


def test_slow_snapshot_resolve_does_not_block_worker_startup(monkeypatch):
    """Медленный resolve_snapshot_url() не должен задерживать старт нити камеры.

    resolve_snapshot_url() делает синхронный ONVIF-вызов get_profiles() с
    таймаутом до 5 с. Раньше он звался ПЕРВЫМ действием camera_worker(), до
    открытия захвата, поэтому на медленной/недоступной камере старт всей нити
    (и старт onvif_poll_worker вместе с ним) сдвигался на всю длину таймаута —
    класс «зелено локально (камера рядом), красно/медленно на объекте», см.
    цикл 60, carryover 14/31. Теперь resolve уехал в фоновую нить после
    открытия захвата; горячий путь старта его не ждёт.

    Проверяется откатом: resolve_snapshot_url висит 6 с (дольше прежней
    границы wait(5)); onvif_poll_worker обязан стартовать задолго до этого.
    Если вернуть вызов на синхронный путь, started.wait(2.0) провалится."""
    started = threading.Event()
    monkeypatch.setattr(worker, "open_capture", lambda url: _OpensThenStallsCapture())
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "load_cam_state", lambda cam_id: (None, True, None, None))
    monkeypatch.setattr(worker, "shutdown_event", threading.Event())

    resolve_started = threading.Event()

    def slow_resolve(*a, **kw):
        resolve_started.set()
        # Дольше прежней границы wait(5): если resolve снова окажется на
        # синхронном пути, старт нити не уложится в started.wait(2.0) ниже.
        for _ in range(60):
            if worker.shutdown_event.is_set():
                break
            worker.shutdown_event.wait(0.1)
        return None

    monkeypatch.setattr(worker, "resolve_snapshot_url", slow_resolve)

    def fake_onvif_poll_worker(*args, **kwargs):
        started.set()
        while not worker.shutdown_event.is_set():
            worker.shutdown_event.wait(0.05)

    monkeypatch.setattr(worker, "onvif_poll_worker", fake_onvif_poll_worker)

    t = threading.Thread(
        target=worker.camera_worker,
        args=(1, "rtsp://cam/main", None),
        kwargs={"sub_rtsp_url": "rtsp://cam/sub", "onvif_config": ONVIF_CONFIG},
        daemon=True,
    )
    t.start()
    try:
        assert started.wait(timeout=2.0), (
            "onvif_poll_worker должен стартовать сразу после открытия захвата, "
            "не дожидаясь медленного resolve_snapshot_url — иначе сетевой вызов "
            "снова на горячем пути старта нити"
        )
        # И сам resolve действительно был запущен (в фоне), а не пропущен.
        assert resolve_started.wait(timeout=2.0), "фоновый resolve_snapshot_url должен быть запущен"
    finally:
        worker.shutdown_event.set()
        t.join(timeout=10)
        assert not t.is_alive(), "camera_worker() не остановился по shutdown — нить утекла бы в соседние тесты"


def test_camera_worker_skips_onvif_thread_without_onvif_config(monkeypatch):
    """Без onvif_config (обычная камера без ONVIF) нить не должна стартовать
    вообще — ни при провале, ни при успехе открытия."""
    started = []
    monkeypatch.setattr(worker, "open_capture", lambda url: _NeverOpensCapture())
    monkeypatch.setattr(worker, "update_status", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "onvif_poll_worker", lambda *a, **kw: started.append(a))

    worker.camera_worker(1, "rtsp://cam/main", face_app=None, sub_rtsp_url=None, onvif_config=None)

    assert started == []
