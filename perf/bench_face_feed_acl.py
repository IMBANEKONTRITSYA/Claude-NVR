#!/usr/bin/env python3
"""Стоимость фильтрации ленты лиц по роли (SPEC §18, §4, §19).

Зачем замер. Фильтр из `backend/app/services/face_feed_acl.py` стоит на
горячем пути: сообщение `box` уходит на **каждый кадр** каждой камеры
analytics, чтобы живая сетка §4 рисовала рамку без задержки. Для ролей,
которым разрешены карточки персон, фильтр отдаёт исходную строку без
разбора — стоимость там заведомо нулевая; для наблюдателя он разбирает
JSON и собирает подмножество полей заново. Вопрос замера один: не съедает
ли эта пересборка бюджет §19 «задержка live ≤ 3 с».

Порядок величин для сверки: при 120 камерах и 5 FPS (§19, потолок слоя
аналитики) лента даёт 600 сообщений в секунду на подписчика.

Запуск:

    python perf/bench_face_feed_acl.py            # человекочитаемо
    python perf/bench_face_feed_acl.py --json     # для CI
"""
import argparse
import json
import os
import platform
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

from app.services.face_feed_acl import filter_face_message  # noqa: E402

# Сообщение `face` — самое дорогое для фильтра: полей больше всего, и
# именно из него вырезается снимок с тегами. `box` идёт чаще, но он короче,
# поэтому меряется худший случай.
FACE_MSG = json.dumps({
    "type": "face",
    "event_id": 1234567,
    "camera_id": 42,
    "person_id": 987,
    "name": "Иванов Иван Иванович",
    "is_known": True,
    "alert": True,
    "tags": ["watchlist", "подрядчик", "смена-2"],
    "snapshot": "cam42_1754380000.jpg",
    "ts": "2026-08-23T00:00:00+00:00",
    "bbox": {"x1": 314, "y1": 208, "x2": 476, "y2": 402},
    "frame_w": 1280,
    "frame_h": 720,
})

# §19: 120 камер × 5 FPS — потолок слоя аналитики на объекте.
MESSAGES_PER_SEC_AT_SPEC_CEILING = 600


def _run(role: str, n: int) -> float:
    """Возвращает микросекунды на сообщение."""
    t0 = time.perf_counter()
    for _ in range(n):
        filter_face_message(FACE_MSG, role)
    return (time.perf_counter() - t0) / n * 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--messages", type=int, default=200_000)
    ap.add_argument("--repeat", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out: dict = {
        "cpu": platform.processor() or platform.machine(),
        "messages": args.messages,
        "repeat": args.repeat,
    }
    for role, label in (("operator", "card_role"), ("viewer", "viewer")):
        runs = [_run(role, args.messages) for _ in range(args.repeat)]
        us = statistics.median(runs)
        out[label] = {
            "us_per_message": round(us, 3),
            "runs_us": [round(x, 3) for x in runs],
            # Доля одного ядра, если лента идёт на потолке §19.
            "core_fraction_at_spec_ceiling": round(
                us * MESSAGES_PER_SEC_AT_SPEC_CEILING / 1e6, 5),
        }

    # Наблюдатель платит за разбор и пересборку; роль с карточками — нет.
    out["viewer_overhead_us"] = round(
        out["viewer"]["us_per_message"] - out["card_role"]["us_per_message"], 3)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"CPU: {out['cpu']}")
    print(f"Сообщений на замер: {args.messages}, повторов: {args.repeat}")
    for label, title in (("card_role", "admin/operator (без разбора)"),
                         ("viewer", "наблюдатель (разбор + пересборка)")):
        d = out[label]
        print(f"  {title:38} {d['us_per_message']:8.3f} мкс/сообщение   "
              f"{d['core_fraction_at_spec_ceiling'] * 100:6.3f} % ядра "
              f"при {MESSAGES_PER_SEC_AT_SPEC_CEILING} сообщ/с")
    print(f"  Надбавка за фильтрацию: {out['viewer_overhead_us']:.3f} мкс/сообщение")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
