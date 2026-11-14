#!/usr/bin/env python3
"""Замер §2: чего стоит слою записи медленная загрузка модели аналитики.

**Что меряется.** Задержка до первого прохода управляющего контура слоя
записи — синхронизации путей MediaMTX, индексации сегментов, публикации
статусов потоков, циклической перезаписи, — когда загрузка модели
аналитики идёт медленно. §2 требует от слоёв не просто независимости при
отказе, а отсутствия общих узких мест: «падение/**деградация** одного не
затрагивает другой».

**Почему это не синтетика.** Загрузка модели медленна на объекте штатно, а
не в исключительных случаях:

* `insightface` тянет пак модели через `requests.get(url, stream=True)`
  **без единого таймаута** (`insightface/utils/download.py`): ни на
  соединение, ни на чтение;
* на свежей установке (§26 режим 2) он качается целиком — при первом
  старте, то есть ровно тогда, когда архива ещё нет ни секунды;
* за файрволом с политикой DROP (для NVR это норма) соединение не
  отвергается, а пропадает: ядро исчерпывает бюджет ретраев SYN
  (`tcp_syn_retries`, по умолчанию 6) — порядка 127 секунд на попытку;
* на вставшем чтении (прокси, throttling) вызов не кончается **никогда**.

**Как меряется.** Настоящий `manager()` воркера с заглушками вокруг: БД
пустая, MediaMTX не нужен, загрузка модели заменена на функцию, которая
держится заданное время. Секундомер — от старта менеджера до первого
вызова `record_layer_sync()`. Сам менеджер, порядок его шагов и обе нити
настоящие; подменено только то, что ходит наружу.

Скрипт печатает две строки: как ведёт себя код сейчас и как вёл бы себя
синхронный вариант (загрузка вызывается из нити менеджера, как до цикла
55). Второе число не оценка, а такой же замер — тем же секундомером на
том же менеджере, с одной переставленной строкой.

    python3 perf/bench_model_load_stall.py --hold 20
    python3 perf/bench_model_load_stall.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "worker"))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("WORKER_WATCHDOG_ENABLED", "0")


class _FakeSession:
    """Пустая БД: меряется порядок работы менеджера, а не содержимое."""

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


def _stub(worker, release: threading.Event, hold: float, sync_at: dict,
          reached: threading.Event):
    import embed_api

    worker.FACE_APP = None
    worker.MODEL_ERROR = None
    worker.shutdown_event = threading.Event()
    worker.Session = _FakeSession
    worker.refresh_config = lambda: None
    worker.log_record_root = lambda: None
    worker.start_watchdog = lambda hb: None
    worker.RECORD_RECOVERY_INTERVAL = 0.0
    worker.index_record_segments = lambda: None
    worker.publish_record_layer_status = lambda names: {}
    worker.check_disk_alerts = lambda: None
    worker.enforce_disk_quota = lambda: 0
    worker.prune_motionless_segments = lambda: 0
    worker.cleanup_old = lambda: None
    worker.prune_orphan_media = lambda: {}
    # Пакетная кластеризация уходит в фоновую нить и на пустой заглушке БД
    # сыплет трассой в вывод. К замеру она отношения не имеет.
    worker._recluster_bg = lambda: None
    embed_api.start_embed_api = lambda *a, **k: None

    def slow_load(*a, **k):
        release.wait(hold)
        return object()

    worker.load_face_app = slow_load

    def _sync(cams):
        sync_at.setdefault("t", time.monotonic())
        reached.set()

    worker.record_layer_sync = _sync


def measure(hold: float, synchronous: bool) -> float:
    """Секунды от старта менеджера до первого прохода слоя записи."""
    import worker

    release = threading.Event()
    reached = threading.Event()
    sync_at: dict[str, float] = {}
    _stub(worker, release, hold, sync_at, reached)

    if synchronous:
        # Поведение до цикла 55: загрузка вызывается из нити менеджера.
        # Переставляется ровно одна строка — та самая, ради которой
        # заведён замер.
        original = worker.ModelLoader

        class _Blocking(original):
            def start(self):
                worker._try_load_model(worker.want_model_params())
                return super().start()

        worker.ModelLoader = _Blocking

    started = time.monotonic()
    t = threading.Thread(target=worker.manager, daemon=True)
    t.start()
    ok = reached.wait(hold + 60)
    release.set()
    worker.shutdown_event.set()
    t.join(timeout=20)
    if synchronous:
        worker.ModelLoader = original
    if not ok:
        raise SystemExit("слой записи не отработал ни разу")
    return sync_at["t"] - started


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=20.0,
                    help="сколько секунд держится загрузка модели")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.json:
        # Журнал воркера идёт в stdout структурированными строками и в
        # режиме --json смешался бы с результатом: вызывающий (джоба CI)
        # читает вывод целиком.
        import logging
        logging.disable(logging.CRITICAL)

    # Порядок важен: синхронный прогон оставляет модель загруженной, и
    # обратный порядок мерил бы второй прогон уже с готовой моделью.
    now = measure(args.hold, synchronous=False)
    before = measure(args.hold, synchronous=True)

    result = {
        "hold_sec": args.hold,
        "record_layer_first_pass_sec": {"async_loader": round(now, 2),
                                        "synchronous_load": round(before, 2)},
        "stall_sec": round(before - now, 2),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return

    print(f"Загрузка модели держится {args.hold:.0f} с "
          f"(на объекте — минуты; см. шапку скрипта)")
    print(f"  синхронная загрузка в нити менеджера : слой записи ждёт "
          f"{before:6.2f} с")
    print(f"  загрузка в своей нити (цикл 55)      : слой записи ждёт "
          f"{now:6.2f} с")
    print(f"  простой управляющего контура записи  : {before - now:6.2f} с "
          f"из {args.hold:.0f} с загрузки")
    print()
    print("Норматив §19 «0 пропущенных сегментов» относится к штатной "
          "работе; первый старт системы штатен.")


if __name__ == "__main__":
    main()
