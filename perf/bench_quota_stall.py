#!/usr/bin/env python3
"""Замер §2: чего стоит контуру записи долгий проход циклической перезаписи.

**Что меряется.** Промежуток между двумя соседними публикациями статусов
слоя записи (`publish_record_layer_status`), когда `enforce_disk_quota()`
идёт долго. Этот этап стоит в цикле менеджера ПОСЛЕДНИМ, поэтому его
длительность добавляется целиком к паузе перед всем следующим проходом:
синхронизацией путей в MediaMTX, индексацией сегментов и публикацией
статусов. §2 требует от слоёв не просто независимости при отказе, а
отсутствия общих узких мест: «падение/**деградация** одного не затрагивает
другой».

**Почему это не синтетика и почему отдельно от `bench_cleanup_stall.py`.**
Тот замер мерил обратное направление — как долгая уборка задерживает
перезапись, — и по его итогам уборка ушла в свою нить (цикл 57). Строка
`"disk_quota": 900.0` при этом осталась в таблице бюджетов сторожа
живости, страницей ниже объяснения, почему такой строки там быть не
должно. Три отличия перезаписи от уборки — все не в пользу прежнего
порядка:

* **частота:** уборка идёт раз в час, перезапись — каждым проходом
  менеджера (~10 с), и ровно потому, что «между часовыми проходами 120
  камер успевают дописать ~108 ГБ». Пока том переполнен, долгий проход не
  эпизод, а установившийся режим;
* **момент:** перезапись срабатывает тогда и только тогда, когда места уже
  нет, — то есть когда статусы потоков и алерт «переполнение диска» (§9)
  нужнее всего;
* **предел:** `LIMIT 5000` ограничивает строки, а не время — это до 10 000
  `unlink()` (файл плюс миниатюра) по HDD-массиву.

Ключ `record:layer` живёт в Redis 120 секунд. Простой длиннее этого
означает не «статусы опоздали», а «страница мониторинга погасла целиком»:
интерфейс показывает «воркер не публиковал состояние слоя записи» вместо
статусов всех камер.

**Как меряется.** Настоящий `manager()` воркера с заглушками вокруг: БД
пустая, MediaMTX не нужен, перезапись заменена на функцию, которая
держится заданное время на первом проходе. Секундомер — между первой и
второй публикацией статусов. Сам менеджер, порядок его шагов и нить
перезаписи настоящие; подменено только то, что ходит наружу.

Скрипт печатает два числа: как ведёт себя код сейчас (перезапись в своей
нити) и как вёл бы себя прежний вариант (перезапись вызывается из нити
менеджера). Второе число не оценка, а такой же замер — тем же секундомером
на том же менеджере, с одной переставленной строкой.

    python3 perf/bench_quota_stall.py --hold 20
    python3 perf/bench_quota_stall.py --json
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

# Пауза менеджера между проходами (`shutdown_event.wait(10)` в `manager()`).
# Она и есть пол этого замера: быстрее, чем раз в 10 секунд, статусы не
# публикуются ни при какой правке.
MANAGER_PASS_SEC = 10.0

# TTL ключа `record:layer` в Redis (`publish_record_layer_status`). Простой
# длиннее — это погасшая страница мониторинга, а не опоздавшие статусы.
RECORD_LAYER_TTL_SEC = 120.0


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
          publishes: list, second: threading.Event):
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
    worker.check_disk_alerts = lambda: None
    worker.prune_motionless_segments = lambda: 0
    worker.prune_orphan_media = lambda: {}
    worker._recluster_bg = lambda: None
    # Уборка архива к этому замеру отношения не имеет и заглушена целиком:
    # иначе её собственная нить стартовала бы на первом же проходе и
    # добавляла шум в измеряемый промежуток.
    worker.start_cleanup_pass = lambda: True
    embed_api.start_embed_api = lambda *a, **k: None

    def _publish(names):
        publishes.append(time.monotonic())
        if len(publishes) >= 2:
            second.set()
        return {}

    worker.publish_record_layer_status = _publish

    held = threading.Event()

    def slow_quota():
        # Держит только ПЕРВЫЙ проход: держи он каждый, менеджер не дошёл бы
        # до второй публикации ни в одном из вариантов, и мерить было бы
        # нечего. На объекте затянут как раз проходы на переполненном томе.
        if held.is_set():
            return 0
        held.set()
        release.wait(hold)
        return 0

    worker.enforce_disk_quota = slow_quota


def measure(hold: float, synchronous: bool) -> float:
    """Секунды между первой и второй публикацией статусов слоя записи."""
    import worker

    release = threading.Event()
    second = threading.Event()
    publishes: list[float] = []
    _stub(worker, hold, release, publishes, second)

    original = worker.start_quota_pass
    if synchronous:
        # Поведение до этого цикла: перезапись вызывается из нити менеджера.
        # Переставляется ровно одна строка — та, ради которой заведён замер.
        def _inline() -> bool:
            worker.enforce_disk_quota()
            return True
        worker.start_quota_pass = _inline

    t = threading.Thread(target=worker.manager, daemon=True)
    t.start()
    ok = second.wait(hold + MANAGER_PASS_SEC + 60)
    release.set()
    worker.shutdown_event.set()
    t.join(timeout=20)
    worker.start_quota_pass = original
    if not ok:
        raise SystemExit("статусы слоя записи не опубликовались дважды")
    return publishes[1] - publishes[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hold", type=float, default=20.0,
                    help="сколько секунд держится проход циклической перезаписи")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.json:
        import logging
        logging.disable(logging.CRITICAL)

    # Порядок: асинхронный вариант первым. Оба прогона независимы
    # (перезапись заглушена), но так вывод читается сверху вниз «сейчас/было».
    now = measure(args.hold, synchronous=False)
    before = measure(args.hold, synchronous=True)

    # Самопроверка замера (carryover 19 цикла 55: «длительность шага CI —
    # непроверяемый никем признак»). Синхронный вариант ОБЯЗАН прождать всю
    # перезапись сверх обычной паузы менеджера: если он уложился быстрее,
    # значит подмена `start_quota_pass` не сработала — например, менеджер
    # перестал звать её по этому имени, — и обе цифры ниже описывают один и
    # тот же код. Молчаливо напечатать «простой 0.00 с» в этом случае хуже,
    # чем упасть: ровно так цикл 55 нашёл два замера, годами отчитывавшихся
    # зелёными и не мерявших ничего.
    if before < (MANAGER_PASS_SEC + args.hold) * 0.9:
        raise SystemExit(
            f"замер недействителен: синхронный вариант дал промежуток "
            f"{before:.2f} с при перезаписи в {args.hold:.0f} с и паузе "
            f"менеджера {MANAGER_PASS_SEC:.0f} с — подмена не сработала, "
            "сравнивать нечего"
        )
    # Вторая самопроверка, с другого конца: асинхронный вариант обязан
    # уложиться в паузу менеджера с запасом. Если он вырос до синхронного —
    # значит нить перезаписи не отвязалась от прохода, и «после» описывает
    # то же поведение, что «до».
    if now > MANAGER_PASS_SEC + args.hold * 0.5:
        raise SystemExit(
            f"замер недействителен: асинхронный вариант дал промежуток "
            f"{now:.2f} с — перезапись всё ещё держит проход менеджера"
        )

    result = {
        "hold_sec": args.hold,
        "record_status_interval_sec": {"background_thread": round(now, 2),
                                       "inline_in_manager": round(before, 2)},
        "stall_sec": round(before - now, 2),
        "record_layer_ttl_sec": RECORD_LAYER_TTL_SEC,
    }
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return

    print(f"Проход циклической перезаписи держится {args.hold:.0f} с "
          f"(на объекте — до 10 000 unlink() по HDD; см. шапку скрипта)")
    print(f"  перезапись в нити менеджера (было): статусы молчат "
          f"{before:6.2f} с")
    print(f"  перезапись в своей нити (этот цикл): статусы молчат "
          f"{now:6.2f} с")
    print(f"  простой управляющего контура записи: {before - now:6.2f} с "
          f"из {args.hold:.0f} с перезаписи")
    print()
    print(f"Порог, за которым простой перестаёт быть опозданием статусов и "
          f"становится погасшей страницей мониторинга, — TTL ключа "
          f"`record:layer`: {RECORD_LAYER_TTL_SEC:.0f} с.")
    print("Перезапись зовётся каждым проходом менеджера, поэтому на "
          "переполненном томе этот простой — не эпизод, а режим.")


if __name__ == "__main__":
    main()
