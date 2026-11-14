#!/usr/bin/env python3
"""Замер §2: чего стоит контуру записи долгая уборка архива по retention.

**Что меряется.** Задержка до первого прохода тех этапов управляющего
контура слоя записи, что стоят в цикле менеджера ПОСЛЕ уборки, — прежде
всего циклической перезаписи (`enforce_disk_quota`) и публикации статусов
потоков (`publish_record_layer_status`), — когда уборка архива идёт долго.
§2 требует от слоёв не просто независимости при отказе, а отсутствия общих
узких мест: «падение/**деградация** одного не затрагивает другой».

**Почему это не синтетика.** `cleanup_old()` — единственный этап прохода
без предела на объём работы: у циклической перезаписи `LIMIT 5000`, у
уборки по движению `LIMIT 1000`, а retention берёт всё просроченное и
делает по два `unlink()` на сегмент. На 120 камерах суточная порция —
десятки тысяч файлов по HDD, и бюджет этапа в сторожe живости стоял ровно
под это: 900 секунд. Пока проход шёл в нити менеджера, столько же не
выполнялась циклическая перезапись — на переполненном томе (тот самый
случай, когда VBR разошёлся с расчётом хранения) запись встала бы целиком.

**Как меряется.** Настоящий `manager()` воркера с заглушками вокруг: БД
пустая, MediaMTX не нужен, уборка заменена на функцию, которая держится
заданное время. Секундомер — от старта менеджера до первого вызова
`enforce_disk_quota()`. Сам менеджер, порядок его шагов и нить уборки
настоящие; подменено только то, что ходит наружу.

Скрипт печатает два числа: как ведёт себя код сейчас (уборка в своей нити)
и как вёл бы себя прежний вариант (уборка вызывается из нити менеджера).
Второе число не оценка, а такой же замер — тем же секундомером на том же
менеджере, с одной переставленной строкой.

    python3 perf/bench_cleanup_stall.py --hold 20
    python3 perf/bench_cleanup_stall.py --json
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


def _stub(worker, hold: float, release: threading.Event,
          quota_at: dict, reached: threading.Event):
    import embed_api

    worker.FACE_APP = object()          # модель «загружена» — этот замер не про неё
    worker.MODEL_ERROR = None
    worker.shutdown_event = threading.Event()
    worker.Session = _FakeSession
    worker.refresh_config = lambda: None
    worker.log_record_root = lambda: None
    worker.start_watchdog = lambda hb: None
    worker.RECORD_RECOVERY_INTERVAL = 0.0
    worker.record_layer_sync = lambda cams: None
    worker.index_record_segments = lambda: None
    worker.publish_record_layer_status = lambda names: {}
    worker.check_disk_alerts = lambda: None
    worker.prune_motionless_segments = lambda: 0
    worker.prune_orphan_media = lambda: {}
    worker._recluster_bg = lambda: None
    embed_api.start_embed_api = lambda *a, **k: None

    def slow_cleanup():
        release.wait(hold)

    worker.cleanup_old = slow_cleanup

    def _quota():
        quota_at.setdefault("t", time.monotonic())
        reached.set()
        return 0

    worker.enforce_disk_quota = _quota


def measure(hold: float, synchronous: bool) -> float:
    """Секунды от старта менеджера до первого прохода циклической перезаписи."""
    import worker

    release = threading.Event()
    reached = threading.Event()
    quota_at: dict[str, float] = {}
    _stub(worker, hold, release, quota_at, reached)

    original = worker.start_cleanup_pass
    if synchronous:
        # Поведение до цикла 57: уборка вызывается из нити менеджера.
        # Переставляется ровно одна строка — та, ради которой заведён замер.
        def _inline() -> bool:
            worker.cleanup_old()
            worker.prune_orphan_media()
            return True
        worker.start_cleanup_pass = _inline

    started = time.monotonic()
    t = threading.Thread(target=worker.manager, daemon=True)
    t.start()
    ok = reached.wait(hold + 60)
    release.set()
    worker.shutdown_event.set()
    t.join(timeout=20)
    worker.start_cleanup_pass = original
    if not ok:
        raise SystemExit("циклическая перезапись не отработала ни разу")
    return quota_at["t"] - started


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=20.0,
                    help="сколько секунд держится уборка архива")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.json:
        import logging
        logging.disable(logging.CRITICAL)

    # Порядок: асинхронный вариант первым. Оба прогона независимы (уборка
    # заглушена), но так вывод читается сверху вниз «сейчас / было».
    now = measure(args.hold, synchronous=False)
    before = measure(args.hold, synchronous=True)

    # Самопроверка замера (carryover 19 цикла 55: «длительность шага CI —
    # непроверяемый никем признак»). Синхронный вариант ОБЯЗАН прождать всю
    # уборку: если он уложился быстрее, значит подмена `start_cleanup_pass`
    # не сработала — например, менеджер перестал звать её по этому имени, —
    # и обе цифры ниже описывают один и тот же код. Молчаливо напечатать
    # «простой 0.00 с» в этом случае хуже, чем упасть: ровно так цикл 55
    # нашёл два замера, годами отчитывавшихся зелёными и не мерявших ничего.
    if before < args.hold * 0.9:
        raise SystemExit(
            f"замер недействителен: синхронный вариант прождал {before:.2f} с "
            f"при уборке в {args.hold:.0f} с — подмена не сработала, "
            "сравнивать нечего"
        )

    result = {
        "hold_sec": args.hold,
        "disk_quota_first_pass_sec": {"background_thread": round(now, 2),
                                      "inline_in_manager": round(before, 2)},
        "stall_sec": round(before - now, 2),
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return

    print(f"Уборка архива держится {args.hold:.0f} с "
          f"(на объекте — минуты; см. шапку скрипта)")
    print(f"  уборка в нити менеджера (было)   : перезапись ждёт "
          f"{before:6.2f} с")
    print(f"  уборка в своей нити (цикл 57)    : перезапись ждёт "
          f"{now:6.2f} с")
    print(f"  простой циклической перезаписи   : {before - now:6.2f} с "
          f"из {args.hold:.0f} с уборки")
    print()
    print("На переполненном томе (§5: VBR разошёлся с расчётом хранения) "
          "этот простой — время, в течение которого запись стоит целиком.")


if __name__ == "__main__":
    main()
