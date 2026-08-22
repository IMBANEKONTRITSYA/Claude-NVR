"""Супервизор восстановления потоков слоя записи (SPEC §19, §13, §2).

Цикл 40 измерил §19 «восстановление потока ≤ 5 секунд после обрыва» и
получил 6.6 с: MediaMTX повторяет подключение к статическому источнику раз
в 5 секунд, и эта пауза одна съедает бюджет норматива. `stream_recovery.py`
обходит её — опрашивает камеру и пересоздаёт путь в тот момент, когда она
ответила.

Здесь проверяются **решения**: кого опрашивать, когда пинать, когда
затухать. Настоящий RTSP-ответ проверяется тем же способом, что и в
`test_onvif_client.py`, — против настоящего сокета, а не мока, потому что
разбор строки состояния и есть то, что отличает живую камеру от мёртвой.
Поведение против настоящего MediaMTX — в `test_stream_recovery_live.py`.

Модуль на одном stdlib, поэтому набор прогоняется в лёгкой CI-джобе
`worker`, где нет ни cv2, ни insightface.
"""
import socket
import threading
import time

import pytest

from record_layer import MediaMTXError
from stream_recovery import (
    KICK_BASE_SEC,
    PROBE_BASE_SEC,
    PROBE_MAX_SEC,
    PROBE_WORKERS,
    PROBE_WORKERS_MAX,
    probe_pool_size,
    RecoveryPlanner,
    RecoverySupervisor,
    down_paths,
    kick_path,
    recover_once,
    rtsp_alive,
)


def _conf(source: str) -> dict:
    return {"source": source, "sourceOnDemand": False, "record": True}


def _live(flag: bool) -> dict:
    """Рантайм-состояние пути в форме настоящего ответа MediaMTX.

    `online: True` стоит в обеих ветках намеренно: у пути со статическим
    источником сервер держит его истинным и во время обрыва (см. шапку
    `record_status.py`). Супервизор, спрашивающий `online`, не увидел бы
    ни одного обрыва — эта фикстура сторожит именно это.
    """
    return {"available": flag, "ready": flag, "online": True}


DESIRED = {"cam1": _conf("rtsp://cam-one/main"), "cam2": _conf("rtsp://cam-two/main")}


class FakeClient:
    """Двойник Control API: помнит порядок вызовов, потому что именно
    порядок `delete` → `add` и есть предмет проверки."""

    def __init__(self, runtime=None, fail_add=False):
        self.runtime = runtime or {}
        self.calls: list[tuple[str, str]] = []
        self.fail_add = fail_add

    def runtime_paths(self):
        return dict(self.runtime)

    def delete_path(self, name):
        self.calls.append(("delete", name))

    def add_path(self, name, conf):
        self.calls.append(("add", name))
        if self.fail_add:
            raise MediaMTXError("add failed")


# --- какие пути считаются оборванными --------------------------------------


def test_offline_path_is_down():
    runtime = {"cam1": _live(True), "cam2": _live(False)}
    assert down_paths(DESIRED, runtime) == {"cam2": "rtsp://cam-two/main"}


def test_missing_path_is_down():
    """Путь заведён в конфигурации, но обработчика нет — это тоже обрыв, и
    лечится он тем же пересозданием."""
    assert down_paths(DESIRED, {"cam1": _live(True)}) == {
        "cam2": "rtsp://cam-two/main"}


def test_unavailable_control_api_is_not_an_outage():
    """`runtime is None` — Control API молчит. Считать это обрывом всех
    камер значило бы пересоздавать 120 путей на каждом рестарте
    медиасервера; та же логика, что в record_status.stream_states()."""
    assert down_paths(DESIRED, None) == {}


def test_non_rtsp_source_is_left_alone():
    """Ручная публикация при диагностике (`source: publisher`) — не наша
    камера: опрашивать нечего, пересоздавать не наше дело."""
    desired = {"cam9": {"source": "publisher"}}
    assert down_paths(desired, {}) == {}


# --- расписание опроса ------------------------------------------------------


def test_first_probe_is_immediate():
    """Обрыв замечен — опрос идёт сразу: задержка растёт на неудачах, а не
    до первой попытки, иначе §19 терял бы на старте секунды впустую."""
    planner = RecoveryPlanner()
    down = {"cam2": "rtsp://cam-two/main"}
    assert planner.due_probes(down, 100.0) == [("cam2", "rtsp://cam-two/main")]


def test_probe_delay_grows_exponentially_and_is_capped():
    planner = RecoveryPlanner()
    down = {"cam2": "rtsp://cam-two/main"}
    now = 100.0
    seen = []
    for _ in range(6):
        assert planner.due_probes(down, now)
        planner.probed("cam2", False, now)
        nxt = planner._state["cam2"]["next_probe_at"]
        seen.append(round(nxt - now, 3))
        now = nxt
    assert seen[:3] == [PROBE_BASE_SEC, PROBE_BASE_SEC * 2, PROBE_BASE_SEC * 4]
    # Потолок — прямое следствие §19: к паузе добавляется ~1.5 с на
    # подключение, и всё вместе обязано уложиться в 5 с.
    assert seen[-1] == PROBE_MAX_SEC
    assert max(seen) <= PROBE_MAX_SEC


def test_probe_is_not_repeated_before_its_time():
    planner = RecoveryPlanner()
    down = {"cam2": "rtsp://cam-two/main"}
    planner.due_probes(down, 100.0)
    planner.probed("cam2", False, 100.0)
    assert planner.due_probes(down, 100.0 + PROBE_BASE_SEC / 2) == []
    assert planner.due_probes(down, 100.0 + PROBE_BASE_SEC) == [
        ("cam2", "rtsp://cam-two/main")]


# --- когда пересоздавать путь ----------------------------------------------


def test_kick_only_when_camera_answers():
    """Пинок по молчащей камере ничего не чинит, а конфигурацию рвёт."""
    planner = RecoveryPlanner()
    planner.due_probes({"cam2": "rtsp://x"}, 100.0)
    assert planner.probed("cam2", False, 100.0) is False
    assert planner.probed("cam2", True, 101.0) is True


def test_kick_delay_grows_when_camera_answers_but_recording_stays_down():
    """Камера отвечает на RTSP, а запись не идёт (сменился пароль, кодек,
    путь в URL) — отказ не в тайминге, и повторять пересоздание раз в
    секунду вредно. §13: задержка растёт."""
    planner = RecoveryPlanner()
    down = {"cam2": "rtsp://x"}
    now = 100.0
    delays = []
    for _ in range(4):
        planner.due_probes(down, now)
        assert planner.probed("cam2", True, now) is True
        planner.kicked("cam2", now)
        nxt = planner._state["cam2"]["next_kick_at"]
        delays.append(round(nxt - now, 3))
        # до следующего пинка опросы идут, но пинков не дают
        assert planner.probed("cam2", True, now + 0.5) is False
        now = nxt
    assert delays[0] == KICK_BASE_SEC
    assert delays[1] == KICK_BASE_SEC * 2
    assert delays == sorted(delays), "задержка обязана расти, а не скакать"


def test_recovered_camera_forgets_its_backoff():
    """Камера, которую однажды чинили полчаса, обязана после следующего
    моргания восстанавливаться так же быстро, как любая другая: иначе §19
    для неё нарушался бы навсегда."""
    planner = RecoveryPlanner()
    down = {"cam2": "rtsp://x"}
    for i in range(5):
        planner.due_probes(down, 100.0 + i)
        planner.probed("cam2", False, 100.0 + i)
    planner.sync({})           # путь снова online — состояние снимается
    assert planner.due_probes(down, 200.0) == [("cam2", "rtsp://x")]
    planner.probed("cam2", False, 200.0)
    assert planner._state["cam2"]["next_probe_at"] == 200.0 + PROBE_BASE_SEC


# --- проход целиком ---------------------------------------------------------


def test_recover_once_kicks_answering_camera_with_delete_then_add():
    """`replace` той же конфигурацией MediaMTX игнорирует (проверено циклом
    40: 19 пинков за обрыв, восстановление те же 6.54 с). Работает только
    пересоздание, и порядок в нём обязателен: `add` на существующий путь
    сервер отвергает."""
    client = FakeClient({"cam1": _live(True), "cam2": _live(False)})
    planner = RecoveryPlanner()
    stats = recover_once(client, DESIRED, client.runtime_paths(), planner,
                         probe=lambda url: True, clock=lambda: 100.0)
    assert client.calls == [("delete", "cam2"), ("add", "cam2")]
    assert stats == {"down": 1, "probed": 1, "alive": 1, "kicked": 1, "failed": 0}


def test_recover_once_takes_every_moment_from_one_clock():
    """Регрессия: проход не смеет смешивать переданное время с
    `time.monotonic()`.

    Пока `due_probes()` получал время от вызывающего, а решение о пинке
    сверялось с `time.monotonic()`, прочитанным внутри, обе величины были
    из разных отсчётов. На машине с большим uptime это незаметно
    (`monotonic()` заведомо больше выдуманного `100.0`), а на свежем
    раннере CI `monotonic()` меньше сотни — и пинок не проходил никогда:
    ровно так этот набор и упал в CI, будучи зелёным в песочнице.

    Часы смещены **вперёд** от `monotonic()`, а не назад и не в
    фиксированную точку: только так проверка падает при смешении часов на
    ЛЮБОЙ машине. С часами, отдающими маленькое число, тест на машине с
    большим uptime зеленел бы и со смешанными часами — то есть повторил бы
    исходную ошибку в самой проверке.
    """
    client = FakeClient({"cam1": _live(True), "cam2": _live(False)})
    ahead = time.monotonic() + 10_000
    stats = recover_once(client, DESIRED, client.runtime_paths(),
                         RecoveryPlanner(), probe=lambda url: True,
                         clock=lambda: ahead)
    assert stats["kicked"] == 1
    assert client.calls == [("delete", "cam2"), ("add", "cam2")]


def test_recover_once_leaves_silent_camera_alone():
    client = FakeClient({"cam1": _live(True), "cam2": _live(False)})
    stats = recover_once(client, DESIRED, client.runtime_paths(), RecoveryPlanner(),
                         probe=lambda url: False, clock=lambda: 100.0)
    assert client.calls == []
    assert stats["kicked"] == 0 and stats["probed"] == 1


def test_recover_once_probes_the_camera_url_not_the_path_name():
    """Опрашивается адрес камеры из конфигурации пути. Если бы сюда попало
    имя пути, проба уходила бы в медиасервер и отвечала бы «жива» всегда."""
    client = FakeClient({"cam1": _live(True), "cam2": _live(False)})
    asked = []
    recover_once(client, DESIRED, client.runtime_paths(), RecoveryPlanner(),
                 probe=lambda url: asked.append(url) or False, clock=lambda: 100.0)
    assert asked == ["rtsp://cam-two/main"]


def test_online_paths_cost_nothing():
    """На здоровом объекте проход не делает ни одного опроса и ни одного
    запроса к Control API сверх чтения состояния — иначе секундный период
    супервизора стоил бы дороже, чем даёт."""
    client = FakeClient({"cam1": _live(True), "cam2": _live(True)})
    asked = []
    stats = recover_once(client, DESIRED, client.runtime_paths(), RecoveryPlanner(),
                         probe=lambda url: asked.append(url) or True, clock=lambda: 100.0)
    assert asked == [] and client.calls == [] and stats["down"] == 0


def test_failed_add_is_counted_and_backed_off():
    """Если `add` не прошёл, путь остался удалённым. Это допустимо ровно
    потому, что sync_paths() менеджера заведёт его на ближайшем проходе, —
    но повторять пересоздание немедленно нельзя."""
    client = FakeClient({"cam1": _live(True), "cam2": _live(False)},
                        fail_add=True)
    planner = RecoveryPlanner()
    stats = recover_once(client, DESIRED, client.runtime_paths(), planner,
                         probe=lambda url: True, clock=lambda: 100.0)
    assert stats["failed"] == 1 and stats["kicked"] == 0
    assert planner._state["cam2"]["kicks"] == 1


def test_kick_survives_missing_path():
    """Путь мог исчезнуть сам (менеджер пересобрал конфигурацию) — тогда
    остаётся просто завести его заново, а не упасть."""
    class Gone(FakeClient):
        def delete_path(self, name):
            self.calls.append(("delete", name))
            raise MediaMTXError("path not found")

    client = Gone()
    kick_path(client, "cam2", DESIRED["cam2"])
    assert client.calls == [("delete", "cam2"), ("add", "cam2")]


# --- проба живой камеры против настоящего сокета ----------------------------


class _RTSPStub:
    """Сокет, отвечающий заданной строкой состояния на DESCRIBE."""

    def __init__(self, response: bytes | None):
        self.response = response
        self.requests: list[bytes] = []
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                try:
                    self.requests.append(conn.recv(1024))
                    if self.response is not None:
                        conn.sendall(self.response)
                except OSError:
                    pass

    def close(self):
        self._sock.close()


@pytest.fixture
def rtsp_stub():
    made = []

    def _make(response):
        stub = _RTSPStub(response)
        made.append(stub)
        return stub

    yield _make
    for stub in made:
        stub.close()


@pytest.mark.parametrize("status,alive", [
    (b"RTSP/1.0 200 OK\r\n\r\n", True),
    # Камера потребовала учётку — значит, её RTSP-сервер поднят и
    # обслуживает запросы. Этого достаточно: учётные данные есть у
    # MediaMTX, а не у пробы.
    (b"RTSP/1.0 401 Unauthorized\r\n\r\n", True),
    (b"RTSP/1.0 403 Forbidden\r\n\r\n", True),
    # 404 отдаёт и MediaMTX на путь без публикатора, и камера, у которой
    # профиль ещё не поднялся, — потока нет.
    (b"RTSP/1.0 404 Not Found\r\n\r\n", False),
    (b"RTSP/1.0 500 Internal\r\n\r\n", False),
    (b"HTTP/1.1 200 OK\r\n\r\n", False),
    (b"", False),
])
def test_rtsp_alive_reads_status_line(rtsp_stub, status, alive):
    stub = rtsp_stub(status)
    assert rtsp_alive(f"rtsp://127.0.0.1:{stub.port}/main", timeout=2.0) is alive


def test_probe_never_sends_credentials(rtsp_stub):
    """Опрос идёт каждые пару секунд, пока камера в обрыве. Учётка слоя
    записи в нём не участвует вовсе: ответ 401 нас устраивает не меньше,
    чем 200, и раскладывать пароль по сети на каждом моргании коммутатора
    незачем."""
    stub = rtsp_stub(b"RTSP/1.0 401 Unauthorized\r\n\r\n")
    url = f"rtsp://admin:sup3rsecret@127.0.0.1:{stub.port}/main"
    assert rtsp_alive(url, timeout=2.0) is True
    sent = b"".join(stub.requests)
    assert b"sup3rsecret" not in sent and b"admin" not in sent
    assert b"Authorization" not in sent
    assert sent.startswith(b"DESCRIBE rtsp://127.0.0.1:")


def test_unreachable_camera_is_not_alive():
    """Недоступная камера — штатное состояние, ради которого модуль и
    написан, а не исключение."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert rtsp_alive(f"rtsp://127.0.0.1:{port}/main", timeout=1.0) is False


def test_silent_camera_does_not_hang_the_probe(rtsp_stub):
    """Камера приняла соединение и молчит — самый неприятный отказ на
    объекте. Проба обязана уложиться в таймаут, иначе один такой поток
    занял бы нить пула навсегда."""
    stub = rtsp_stub(None)
    import time as _t
    started = _t.monotonic()
    assert rtsp_alive(f"rtsp://127.0.0.1:{stub.port}/main", timeout=1.0) is False
    assert _t.monotonic() - started < 3.0


# --- нить супервизора -------------------------------------------------------


def test_supervisor_stops_and_joins():
    """Нить джойнится, а не бросается демоном: между `delete` и `add` путь
    не существует, и оборвать её в этой точке — единственный способ
    оставить камеру без записи до следующего прохода менеджера."""
    client = FakeClient({"cam1": _live(True)})
    sup = RecoverySupervisor(lambda: client, lambda: {"cam1": _conf("rtsp://a/b")},
                             interval=0.05, probe=lambda url: True).start()
    try:
        assert sup._thread.is_alive()
    finally:
        sup.stop(timeout=5.0)
    assert not sup._thread.is_alive()


def test_supervisor_survives_control_api_failure():
    """Медиасервер перезапускается — супервизор обязан пережить это молча
    и продолжить, а не умереть вместе с ним (SPEC §2)."""
    class Broken(FakeClient):
        def runtime_paths(self):
            raise MediaMTXError("connection refused")

    sup = RecoverySupervisor(lambda: Broken(), lambda: {"cam1": _conf("rtsp://a/b")},
                             interval=0.05)
    stats = sup.tick()
    assert stats["api_error"] is True and stats["kicked"] == 0


def test_supervisor_snapshot_shows_what_operator_needs():
    """«Камера выключена» и «камера отвечает, а запись не идёт» на стене
    выглядят одинаково, а чинятся по-разному."""
    client = FakeClient({"cam1": _live(False)})
    sup = RecoverySupervisor(lambda: client, lambda: {"cam1": _conf("rtsp://a/b")},
                             interval=0.05, probe=lambda url: True)
    sup.tick()
    snap = sup.snapshot()
    assert snap[1]["camera_answering"] is True
    assert snap[1]["kicks"] == 1


# --- Пачка обрывов (цикл 44) -------------------------------------------------

def test_probe_pool_scales_with_the_outage():
    """Размер пула опроса — следствие §19, а не константа.

    Пул фиксированной величины (16 нитей до цикла 44) означает, что проход
    опроса длится `камер / 16 × таймаут`: на 120 молчащих камерах это
    16.4 с при бюджете §19 в 5 с — замерено `perf/bench_recovery_batch.py`.
    Пул обязан расти до размера обрыва, чтобы проход укладывался в один
    таймаут.
    """
    assert probe_pool_size(0) == PROBE_WORKERS
    assert probe_pool_size(5) == PROBE_WORKERS
    assert probe_pool_size(120) == 120
    # Потолок: 250+ камер (§1) укладываются в одну волну, но пул не растёт
    # безгранично на испорченном списке путей.
    assert probe_pool_size(10_000) == PROBE_WORKERS_MAX


def test_answering_camera_is_kicked_without_waiting_for_the_silent_ones():
    """Вернувшаяся камера не ждёт очередь из молчащих.

    Это регрессия ровно на `executor.map`, который отдавал результаты
    только после самого медленного опроса: камера, ответившая за
    миллисекунды, ждала таймаута всех остальных. Здесь молчащие держат
    нить искусственно, а проверяется момент пинка относительно них.
    """
    from concurrent.futures import ThreadPoolExecutor

    silent = {f"cam{i}": _conf(f"rtsp://10.0.0.{i}:554/s") for i in range(2, 10)}
    desired = {"cam1": _conf("rtsp://10.0.0.1:554/s"), **silent}
    runtime = {name: _live(False) for name in desired}

    # Молчащие держат нить, пока вернувшуюся не пересоздали. Проход,
    # разбирающий результаты по мере готовности, освобождает их сам и
    # укладывается в доли секунды; проход, ждущий весь пакет, простоит
    # весь SILENT_HOLD. Разрыв между величинами кратный, а не на грани, —
    # иначе тест был бы зелёным и на старом поведении (проверено откатом).
    SILENT_HOLD = 30.0
    MAX_PASS = 5.0

    kicked_at: list[float] = []
    released = threading.Event()

    def probe(url: str) -> bool:
        if url.endswith("10.0.0.1:554/s"):
            return True
        released.wait(timeout=SILENT_HOLD)
        return False

    class Recorder(FakeClient):
        def delete_path(self, name: str) -> None:
            if name == "cam1":
                kicked_at.append(time.monotonic())
                released.set()
            super().delete_path(name)

    client = Recorder(runtime)
    executor = ThreadPoolExecutor(max_workers=16)
    started = time.monotonic()
    try:
        stats = recover_once(client, desired, runtime, RecoveryPlanner(),
                             probe=probe, executor=executor)
    finally:
        executor.shutdown(wait=False)
    elapsed = time.monotonic() - started
    assert kicked_at, "вернувшуюся камеру не пересоздали вовсе"
    assert stats["kicked"] == 1
    assert elapsed < MAX_PASS, (
        f"проход занял {elapsed:.1f} с: вернувшаяся камера ждала молчащих")


def test_supervisor_grows_its_pool_under_a_mass_outage():
    """Пул расширяется по факту обрыва, а не по числу заведённых камер.

    Порядок здесь существенный: узнать масштаб надо ДО прохода. Пул,
    подобранный по итогам прошлого прохода, опоздал бы ровно на тот
    проход, который и длится дольше бюджета §19.
    """
    desired = {f"cam{i}": _conf(f"rtsp://10.0.0.{i}:554/s") for i in range(1, 41)}
    runtime = {name: _live(False) for name in desired}
    # Проходы вызываются вручную: нить супервизора тикает сразу после
    # start(), и к первой же проверке пул был бы уже расширен — тест
    # проходил бы, не проверив ничего.
    sup = RecoverySupervisor(lambda: FakeClient(runtime), lambda: desired,
                             probe=lambda url: False)
    try:
        assert sup._pool_size == PROBE_WORKERS
        sup.tick()
        assert sup._pool_size == 40, "пул не подстроился под масштаб обрыва"
        # Обрыв кончился — пул не сжимается: гасить и заводить нити на
        # каждом моргании дороже, чем держать их простаивающими.
        sup.tick()
        assert sup._pool_size == 40
    finally:
        sup.stop()
