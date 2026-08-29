#!/usr/bin/env python3
"""Цена прохода индексации архива на каталоге боевого размера (SPEC §5, §19).

**Что меряется.** `segment_index.index_new_segments()` — шаг
`index_segments` цикла менеджера воркера, выполняется **каждые ~10 с**.
Меряется установившийся проход, то есть тот, на котором добавлять нечего:
именно он идёт 99.99 % времени работы объекта, и именно его цену платит
сервер постоянно.

**Почему каталог такой большой.** Слой записи кладёт все сегменты в один
плоский каталог `<media>/segments/` (`recordPath` MediaMTX —
`%path_%s`, см. `record_layer.record_path_template`). Размер каталога —
это `камеры × сутки хранения × сегментов в сутки`, и при значениях по
умолчанию (`RETENTION_DAYS_DEFAULT = 30`, сегмент 5 минут, §20 «Большой
объект» — 120 камер) получается **1 036 800 файлов**. Это не худший
случай, а конфигурация из коробки.

**Что показал замер (цикл 53).** До правки установившийся проход:

* снимал `os.stat` с **каждого** файла каталога — миллион системных
  вызовов;
* передавал в БД `file_path IN (...)` со **всеми** миллионом путей;
* и выбрасывал результат целиком, потому что новых сегментов нет.

Правка — отсечка по «докуда архив уже заполнен»: один агрегат
`GROUP BY camera_id, max(started_at)` даёт границу по каждой камере, и
файлы не позже неё отбрасываются **до** `stat`, по одному только имени
(время начала сегмента стоит в имени файла).

**Известное ограничение отсечки** — шаг системных часов назад (коррекция
NTP) больше длины сегмента: записанное в «отмотанном» промежутке имя
файла окажется не позже границы и в архив не попадёт. Прежний код такой
сегмент бы занёс. Цена отсечки принята сознательно: событие редкое и
ограниченное по последствиям, а плата за её отсутствие — постоянная.

Запуск (нужен настоящий Postgres):

    DATABASE_URL=postgresql://... python perf/bench_segment_scan.py [--cameras 120] [--days 30]

Каталог с файлами создаётся во временном месте и удаляется за собой;
на 1 036 800 файлов по 2 КБ это ~2 ГБ и несколько минут на подготовку.
"""
from __future__ import annotations

import argparse
import datetime
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "worker"))

from sqlalchemy import (BigInteger, Column, DateTime, ForeignKey, Integer,  # noqa: E402
                        String, create_engine)
from sqlalchemy.orm import declarative_base, sessionmaker  # noqa: E402

import record_layer  # noqa: E402
from segment_index import index_new_segments  # noqa: E402

Base = declarative_base()

SEGMENT_SEC = 300          # SPEC §20: сегменты 5–10 минут
SEGMENT_BYTES = 2048       # больше MIN_SEGMENT_BYTES; размер файла на цену не влияет


class Camera(Base):
    __tablename__ = "bench_scan_cameras"
    id = Column(Integer, primary_key=True)


class VideoSegment(Base):
    __tablename__ = "bench_scan_segments"
    id = Column(Integer, primary_key=True)
    camera_id = Column(Integer, ForeignKey("bench_scan_cameras.id", ondelete="CASCADE"),
                       index=True)
    started_at = Column(DateTime, index=True)
    ended_at = Column(DateTime)
    file_path = Column(String(500))
    event_type = Column(String(20))
    duration_sec = Column(Integer)
    size_bytes = Column(BigInteger, default=0)


def build_tree(root: str, cameras: int, per_camera: int, base_ts: int) -> int:
    buf = b"\0" * SEGMENT_BYTES
    made = 0
    for cam in range(1, cameras + 1):
        for i in range(per_camera):
            name = f"cam{cam}_{base_ts + i * SEGMENT_SEC}.mp4"
            with open(os.path.join(root, name), "wb") as fh:
                fh.write(buf)
            made += 1
    return made


def timed(fn, runs: int = 3) -> float:
    """Лучшее из `runs` — интересует цена работы, а не шум соседей по хосту."""
    best = float("inf")
    for _ in range(runs):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cameras", type=int, default=120)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--keep", action="store_true", help="не удалять каталог")
    ap.add_argument("--min-speedup", type=float, default=None,
                    help="ненулевой код возврата, если выигрыш ниже. Шаг CI "
                         "гоняет уменьшенный каталог и сторожит именно "
                         "отношение: сломайся отсечка — оно схлопнется к 1×, "
                         "а абсолютные миллисекунды на раннере ничего не "
                         "значат")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL", "").replace("+asyncpg", "")
    if not url or url.startswith("sqlite"):
        print("нужен настоящий Postgres в DATABASE_URL", file=sys.stderr)
        return 2

    per_camera = args.days * 86400 // SEGMENT_SEC
    total = args.cameras * per_camera
    base_ts = 1_700_000_000

    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine)
    with Session() as s:
        for cam in range(1, args.cameras + 1):
            s.add(Camera(id=cam))
        s.commit()

    root = tempfile.mkdtemp(prefix="bench-segments-")
    try:
        print(f"каталог архива: {args.cameras} камер × {args.days} сут × "
              f"{86400 // SEGMENT_SEC} сегм/сут = {total} файлов")
        t = time.perf_counter()
        made = build_tree(root, args.cameras, per_camera, base_ts)
        print(f"  подготовка: {made} файлов за {time.perf_counter() - t:.0f} с")

        common = dict(now=time.time() + 86400 * 400, camera_model=Camera,
                      from_timestamp=datetime.datetime.utcfromtimestamp)

        t = time.perf_counter()
        added = index_new_segments(Session, VideoSegment, root, **common)
        print(f"\nпервичная индексация: {added} строк за "
              f"{time.perf_counter() - t:.0f} с (разовая, при первом запуске)")

        # --- установившийся проход: добавлять нечего --------------------
        after = timed(lambda: index_new_segments(Session, VideoSegment, root, **common))

        # То же самое, но с отключённой отсечкой — как работал код до
        # цикла 53. Подменяется сама функция обхода, чтобы мерить один и
        # тот же путь исполнения.
        orig = record_layer.collect_complete_segments

        def no_watermark(segments_dir, now, settle_sec=record_layer.SEGMENT_SETTLE_SEC,
                         after=None):
            return orig(segments_dir, now, settle_sec, after=None)

        import segment_index
        segment_index.collect_complete_segments = no_watermark
        try:
            before = timed(lambda: index_new_segments(Session, VideoSegment, root, **common))
        finally:
            segment_index.collect_complete_segments = orig

        print("\nустановившийся проход (каждые ~10 с в цикле менеджера):")
        print(f"  до правки : {before * 1000:9.0f} мс")
        print(f"  после     : {after * 1000:9.0f} мс")
        print(f"  выигрыш   : {before / after:9.1f}×")
        print(f"\n  доля одного ядра при интервале 10 с: "
              f"{before / 10 * 100:.1f} % → {after / 10 * 100:.1f} %")
        if before > 10:
            print("  ВНИМАНИЕ: до правки проход дольше самого интервала цикла")
        if args.min_speedup is not None and before / after < args.min_speedup:
            print(f"\nОТКАЗ: выигрыш {before / after:.1f}× ниже порога "
                  f"{args.min_speedup}× — отсечка «докуда архив заполнен» "
                  f"перестала работать", file=sys.stderr)
            return 1
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
