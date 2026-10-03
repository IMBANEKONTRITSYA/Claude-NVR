#!/usr/bin/env python3
"""Замер §19 «восстановление потока ≤ 5 с» на ПАЧКЕ обрывов.

**Почему одного замера цикла 43 недостаточно.** `bench_recovery.py` мерит
одну камеру: она отваливается, возвращается, и восстановление укладывается
в 3.59 с. Но на объекте обрыв одной камеры — редкий случай; типовой отказ,
ради которого §19 и написан, другой: **моргнул коммутатор, и отвалились
все 120 разом**. Carryover цикла 43 записал это прямо: «величина пула
опроса (16 нитей × таймаут 2 с) подобрана из соображений, а не из замера»,
и на 120 молчащих камерах проход опроса арифметически даёт ~15 секунд.

**Что меряется.** Не проход сам по себе, а то, что ограничивает §19:
время от момента, когда ОДНА камера вернулась, до момента, когда
супервизор пересоздал её путь (`kick`). Всё остальное в цепочке
восстановления замерено циклом 43 и от числа камер не зависит; зависит
ровно это — потому что вернувшаяся камера ждёт своей очереди в пуле за
теми, кто ещё молчит.

**Как устроен стенд.** Настоящие `recover_once` и `RecoveryPlanner` из
`worker/stream_recovery.py` — не копия логики, а она сама, по той же
причине, что и в `bench_recovery.py`. Подменяются только две вещи, и обе
снаружи системы:

* «камеры» — локальные TCP-сокеты. Молчащая камера принимает соединение и
  не отвечает ничего: это даёт ровно тот же полный таймаут `PROBE_TIMEOUT_
  SEC`, что и недоступная камера на объекте, и не зависит от сети
  песочницы. Вернувшаяся отвечает `RTSP/1.0 200 OK` немедленно;
* Control API MediaMTX — двойник, считающий вызовы. Здесь он и не нужен
  настоящим: меряется момент, когда до `kick` дошла очередь, а не то,
  сколько MediaMTX его исполняет (это цикл 43 уже замерил).

Запуск:

    python perf/bench_recovery_batch.py                 # 1, 30, 120, 250
    python perf/bench_recovery_batch.py --cameras 240   # свой размер
    python perf/bench_recovery_batch.py --repeat 5      # больше прогонов
    python perf/bench_recovery_batch.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "worker"))

from stream_recovery import (  # noqa: E402
    PROBE_WORKERS, RecoveryPlanner, recover_once)
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

try:  # появился в цикле 48 вместе с неблокирующим опросом
    from stream_recovery import probe_many
except ImportError:  # замер должен работать и на коде «до фикса»
    probe_many = None

try:  # появилась в цикле 44 вместе с фиксом очереди опроса
    from stream_recovery import probe_pool_size
except ImportError:  # замер должен работать и на коде «до фикса»
    def probe_pool_size(down: int) -> int:  # type: ignore[misc]
        return PROBE_WORKERS

# §19: «Восстановление потока ≤ 5 секунд после обрыва».
RECOVERY_BUDGET_SEC = 5.0

# Размеры объекта для замера. 120 — пример из §17 («камеры 1-60 на node 0,
# 61-120 на node 1»); 12 и 250 — границы диапазона §1.
DEFAULT_SIZES = (1, 30, 120, 250)


class FakeCamera:
    """Локальный сокет вместо камеры.

    `alive=False` — принимает соединение и молчит: воспроизводит именно
    тот случай, который стоит дорого (полный таймаут опроса), а не
    отказ в соединении (он возвращается мгновенно и ничего не стоит).
    """

    def __init__(self, alive: bool = False) -> None:
        self.alive = alive
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._conns: list[socket.socket] = []
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = self._sock.accept()
            except OSError:
                return
            self._conns.append(conn)
            if self.alive:
                try:
                    conn.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
                except OSError:
                    pass
            # Молчащая камера соединение держит: закрыть его значило бы
            # отдать опросу мгновенный EOF вместо таймаута.

    @property
    def url(self) -> str:
        return f"rtsp://127.0.0.1:{self.port}/stream"

    def close(self) -> None:
        self._stop.set()
        for conn in self._conns:
            try:
                conn.close()
            except OSError:
                pass
        try:
            self._sock.close()
        except OSError:
            pass


class CountingClient:
    """Двойник Control API: помнит, когда какой путь пересоздали."""

    def __init__(self) -> None:
        self.kicks: dict[str, float] = {}
        self.lock = threading.Lock()

    def delete_path(self, name: str) -> None:
        with self.lock:
            self.kicks.setdefault(name, time.monotonic())

    def add_path(self, name: str, conf: dict) -> None:
        pass

    # Слой записи зовёт kick_path(), который ходит этими двумя методами;
    # остальное API супервизору не нужно.
    def runtime_paths(self) -> dict:
        return {}


def measure(total_cameras: int, workers: int | None = None,
            mode: str = "selector") -> dict:
    """Сколько секунд проходит от возвращения камеры до пересоздания пути.

    Одна камера возвращается, остальные `total_cameras - 1` молчат — то
    есть худший случай для очереди опроса: вернувшаяся стоит в ней
    последней.
    """
    cameras = [FakeCamera(alive=False) for _ in range(total_cameras - 1)]
    # Молчит и она тоже: обрыв начинается у всех сразу, и «вернулась»
    # она только после разогрева. Если завести её живой, разогревочный
    # проход успеет пересоздать путь и включить задержку пинков (5 с), и
    # замер показал бы её, а не очередь опроса.
    returning = FakeCamera(alive=False)
    # Вернувшаяся — последняя в списке: планировщик сохраняет порядок
    # `desired`, и так замер показывает не средний случай, а тот, который
    # ограничивает норматив.
    cameras.append(returning)
    desired = {f"cam{i}": {"source": cam.url, "sourceOnDemand": False}
               for i, cam in enumerate(cameras)}
    runtime: dict[str, dict] = {}  # ни одного живого пути — все в обрыве

    planner = RecoveryPlanner()
    client = CountingClient()
    # Два режима, чтобы «до» и «после» мерились одним стендом:
    #   threads  — пул нитей с потолком PROBE_WORKERS_MAX (до цикла 48);
    #   selector — неблокирующий проход одним потоком (с цикла 48).
    use_selector = mode == "selector" and probe_many is not None
    pool = workers if workers is not None else probe_pool_size(len(desired))
    executor = None if use_selector else ThreadPoolExecutor(
        max_workers=pool, thread_name_prefix="bench-probe")
    batch = probe_many if use_selector else None
    if use_selector:
        pool = 1
    returning_path = f"cam{len(cameras) - 1}"
    try:
        # Разогрев: обрыв у всех, планировщик узнаёт про него и назначает
        # опросы. Камера, которая вернётся, здесь ещё молчит — иначе
        # замер начинался бы уже после её пересоздания.
        recover_once(client, desired, runtime, planner, executor=executor,
                     probe_batch=batch)
        client.kicks.clear()

        # Камера вернулась. Дальше — то же, что делает нить супервизора:
        # проходы раз в TICK_SEC, пока путь не пересоздан.
        returning.alive = True
        started = time.monotonic()
        cpu_started = time.process_time()
        deadline = started + 120
        stats: dict = {}
        pass_sec = 0.0
        while time.monotonic() < deadline:
            pass_started = time.monotonic()
            stats = recover_once(client, desired, runtime, planner,
                                 executor=executor, probe_batch=batch)
            if stats.get("probed"):
                # Интересен проход, в котором опрос действительно шёл:
                # проход без «созревших» опросов выходит мгновенно и о
                # стоимости не говорит ничего.
                pass_sec = max(pass_sec, time.monotonic() - pass_started)
            if returning_path in client.kicks:
                break
            time.sleep(0.2)
        kicked_at = client.kicks.get(returning_path)
        latency = (kicked_at - started) if kicked_at else None
        # Процессорное время всего замера против настенного: опрос стоит
        # сокетов и ожидания, а не ядер, и это утверждение из шапки
        # `PROBE_WORKERS_MAX` должно быть замерено, а не объявлено.
        cpu_sec = time.process_time() - cpu_started
        wall_sec = time.monotonic() - started
    finally:
        if executor is not None:
            executor.shutdown(wait=False)
        for cam in cameras:
            cam.close()

    return {
        "cameras": total_cameras,
        "mode": "selector" if use_selector else "threads",
        "pool": pool,
        "pass_sec": round(pass_sec, 3),
        "cpu_sec": round(cpu_sec, 3),
        "cpu_pct": round(100.0 * cpu_sec / wall_sec, 1) if wall_sec > 0 else None,
        "kick_latency_sec": round(latency, 3) if latency is not None else None,
        "kicked": stats.get("kicked", 0),
        "probed": stats.get("probed", 0),
        "within_budget": bool(latency is not None and latency <= RECOVERY_BUDGET_SEC),
    }


def worst_of(total_cameras: int, workers: int | None, repeat: int,
             mode: str = "selector") -> dict:
    """Худший из `repeat` прогонов — им и проверяется норматив.

    Одиночный прогон здесь **не воспроизводится**, и это свойство самого
    замера, а не шум раннера. После обрыва планировщик назначает каждой
    камере свой момент следующего опроса, и моменты эти расходятся по
    ходу разогревочного прохода (опрос идёт пачкой, ответы приходят
    вразнобой). Попадёт ли вернувшаяся камера в ближайшую партию «созревших»
    или дождётся следующей — вопрос того, где её опрос оказался в
    расписании, а не производительности: отсюда разброс от ~0.2 с до
    потолка паузы опроса `PROBE_MAX_SEC` плюс проход, то есть ~2.5 с.

    Обе величины укладываются в §19, но публиковать надо верхнюю: цикл 44
    сначала записал в отчёт 0.21 с — лучший случай, — и число не
    воспроизвелось на первом же повторном прогоне.
    """
    runs = [measure(total_cameras, workers, mode) for _ in range(max(1, repeat))]
    worst = max(runs, key=lambda r: (r["kick_latency_sec"] is None,
                                     r["kick_latency_sec"] or 0.0))
    latencies = [r["kick_latency_sec"] for r in runs
                 if r["kick_latency_sec"] is not None]
    worst = dict(worst)
    worst["runs"] = len(runs)
    worst["best_sec"] = round(min(latencies), 3) if latencies else None
    return worst


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", type=int, nargs="*", default=list(DEFAULT_SIZES))
    ap.add_argument("--workers", type=int, default=None,
                    help="размер пула опроса (по умолчанию — как в воркере)")
    ap.add_argument("--repeat", type=int, default=3,
                    help="прогонов на размер; в отчёт идёт ХУДШИЙ")
    ap.add_argument("--mode", choices=("selector", "threads"), default="selector",
                    help="selector — неблокирующий проход (боевой с цикла 48); "
                         "threads — прежний пул нитей, для сравнения")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    results = [worst_of(n, args.workers, args.repeat, args.mode) for n in args.cameras]
    if args.json:
        print(json.dumps({"budget_sec": RECOVERY_BUDGET_SEC,
                          "default_pool": PROBE_WORKERS,
                          "results": results}, ensure_ascii=False))
        return 0

    print(f"§19 восстановление потока ≤ {RECOVERY_BUDGET_SEC} с — пачка обрывов")
    print(f"{'камер':>7} {'пул':>5} {'проход, с':>11} {'худшее, с':>11} "
          f"{'лучшее, с':>11} {'CPU, %':>8} {'§19':>6}")
    for r in results:
        latency = "—" if r["kick_latency_sec"] is None else f"{r['kick_latency_sec']:.2f}"
        best = "—" if r.get("best_sec") is None else f"{r['best_sec']:.2f}"
        verdict = "да" if r["within_budget"] else "НЕТ"
        cpu = "—" if r["cpu_pct"] is None else f"{r['cpu_pct']:.1f}"
        print(f"{r['cameras']:>7} {r['pool']:>5} {r['pass_sec']:>11.2f} "
              f"{latency:>11} {best:>11} {cpu:>8} {verdict:>6}")
    print("\nНорматив проверяется по ХУДШЕМУ из прогонов "
          f"(--repeat {args.repeat}).")
    return 0 if all(r["within_budget"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
