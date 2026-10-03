#!/usr/bin/env python3
"""Замер агрегата «границы архива по камерам» и проверка гипотезы об индексе.

**Что меряется.** Запрос «границы записи по каждой камере» —
`GROUP BY camera_id` с `min(started_at)`/`max(ended_at)`, join к `cameras`.
Он лежит на трёх путях сразу:

* §12 ONVIF Profile G — `GetRecordings`, который внешний VMS зовёт первым
  делом, чтобы узнать состав и границы архива (`services/onvif_profile_g.py`);
* §5/§7 — карточки архива и выбор диапазона в плеере;
* §9 — сводка «глубина архива по камерам» в мониторинге.

**Зачем скрипт появился и что он показал.** Цикл 37 занёс в carryover P2
такой пункт: «`GetRecordings` без области — ~1014 мс на 240 000 сегментов;
оптимизация — покрывающий составной индекс
`video_segments(camera_id, started_at, ended_at)` под index-only min/max».
Цикл 38 проверил гипотезу замером, прежде чем её реализовывать, и она
**не подтвердилась по обоим пунктам**:

1. **Числа не воспроизводятся.** На тех же 240 000 сегментах и 120 камерах
   запрос идёт **~180 мс**, а не ~1014 мс. Планировщик берёт параллельный
   `Finalize GroupAggregate`, и это уже эффективно.
2. **Составной индекс не даёт ничего.** План запроса с ним **не меняется**
   (та же оценка стоимости), время — в пределах шума: 0.98× на 240 000 и
   0.98× на 2 400 000. Планировщик его просто не выбирает: агрегат читает
   всю таблицу целиком, и index-only scan по индексу шириной в три колонки
   дешевле seq scan не делается.

   При этом индекс **стоит** ~9.5 МБ на 240 000 строк, то есть ~95 МБ на
   объекте с 2.4 млн сегментов — и замедляет вставку, которая на слое
   записи идёт постоянно.

3. **Рост сублинейный.** Десятикратный рост таблицы (240 тыс → 2.4 млн)
   даёт рост времени в 1.4× (180 → 260 мс) — то есть довод «на миллионах
   строк вырастет» тоже не подтверждается.

**Вывод: индекс добавлять не нужно**, пункт снят с carryover. Запас до
норматива §7 «≤ 5 с» на 2.4 млн сегментов — примерно **19×**.

Скрипт оставлен в репозитории именно как доказательство: без него
следующий цикл заново прочитает пункт в carryover и потратит время на
оптимизацию, которая ничего не ускоряет и занимает диск. Он же пригодится,
когда таблица на объекте вырастет ещё на порядок.

Запуск (нужен Postgres):

    DATABASE_URL=postgresql://facewatch:facewatch@localhost:5432/facewatch_test \
        python perf/bench_archive_index.py [строк] [камер]
"""
import os
import statistics
import sys
import time

import psycopg2

DSN = os.environ.get(
    "BENCH_DSN",
    os.environ.get("DATABASE_URL", "postgresql://facewatch:facewatch@localhost:5432/facewatch_test"),
).replace("postgresql+asyncpg://", "postgresql://").replace("postgresql+psycopg2://", "postgresql://")

SEG = "bench_video_segments"
CAM = "bench_cameras"
INDEX = "idx_bench_segments_camera_bounds"

# §7 «Поиск по архиву ≤ 5 секунд».
LIMIT_MS = 5000

# Форма запроса — ровно та, что уходит в БД из `onvif_profile_g.recordings()`.
# Join к камерам включён намеренно: без него замер отвечал бы на другой
# вопрос, чем тот, что стоит на горячем пути.
QUERY = f"""
SELECT s.camera_id, min(s.started_at), max(s.ended_at), c.name, c.location
FROM {SEG} s JOIN {CAM} c ON c.id = s.camera_id
GROUP BY s.camera_id, c.name, c.location
ORDER BY s.camera_id
"""

# Обычный путь VMS: FindRecordings с областью из нескольких камер.
QUERY_SCOPED = QUERY.replace("GROUP BY", "WHERE s.camera_id = ANY(%s)\nGROUP BY")


def seed(cur, rows: int, cameras: int) -> None:
    """Таблицы той же формы, что боевые (models.py), с теми же индексами."""
    cur.execute(f"DROP TABLE IF EXISTS {SEG} CASCADE")
    cur.execute(f"DROP TABLE IF EXISTS {CAM} CASCADE")
    cur.execute(f"CREATE TABLE {CAM} (id integer PRIMARY KEY, "
                f"name varchar(120), location varchar(120))")
    cur.execute(f"INSERT INTO {CAM} SELECT g, 'Камера '||g, 'Локация' "
                f"FROM generate_series(1, %s) g", (cameras,))
    cur.execute(f"""
        CREATE TABLE {SEG} (
            id bigserial PRIMARY KEY,
            camera_id integer NOT NULL REFERENCES {CAM}(id),
            started_at timestamp NOT NULL,
            ended_at timestamp NOT NULL,
            file_path varchar(500) NOT NULL,
            event_type varchar(20) NOT NULL,
            duration_sec integer NOT NULL DEFAULT 0,
            size_bytes bigint NOT NULL DEFAULT 0
        )
    """)
    # Сегменты по минуте на камеру — так их пишет MediaMTX (§20).
    cur.execute(f"""
        INSERT INTO {SEG}
            (camera_id, started_at, ended_at, file_path, event_type, duration_sec, size_bytes)
        SELECT (g %% %s) + 1,
               timestamp '2026-01-01 00:00:00' + (g / %s) * interval '1 minute',
               timestamp '2026-01-01 00:01:00' + (g / %s) * interval '1 minute',
               'cam' || ((g %% %s) + 1) || '_' || g || '.mp4',
               'continuous', 60, 15000000
        FROM generate_series(0, %s - 1) g
    """, (cameras, cameras, cameras, cameras, rows))
    # Те же три однополевых индекса, что стоят на боевой таблице: сравнение
    # должно идти против того, что есть на объекте, а не против таблицы
    # вообще без индексов.
    cur.execute(f"CREATE INDEX ON {SEG} (camera_id)")
    cur.execute(f"CREATE INDEX ON {SEG} (started_at)")
    cur.execute(f"CREATE INDEX ON {SEG} (ended_at)")
    cur.execute(f"ANALYZE {SEG}")
    cur.execute(f"ANALYZE {CAM}")


def timed(cur, sql, params=None, runs: int = 5) -> tuple[float, float]:
    """Медиана и максимум, мс. Первый прогон — прогрев кэша, в выборку не идёт."""
    def run():
        cur.execute(sql, params) if params else cur.execute(sql)
        cur.fetchall()

    run()
    samples = []
    for _ in range(runs):
        t0 = time.perf_counter()
        run()
        samples.append((time.perf_counter() - t0) * 1000)
    return statistics.median(samples), max(samples)


def plan(cur, sql, params=None) -> str:
    cur.execute("EXPLAIN " + sql, params) if params else cur.execute("EXPLAIN " + sql)
    return cur.fetchone()[0].strip()


def main() -> int:
    rows = int(sys.argv[1]) if len(sys.argv) > 1 else 240_000
    cameras = int(sys.argv[2]) if len(sys.argv) > 2 else 120
    scope = list(range(1, 4))

    conn = psycopg2.connect(DSN)
    conn.autocommit = True
    cur = conn.cursor()

    print(f"засеваю {rows} сегментов на {cameras} камер…")
    seed(cur, rows, cameras)

    print()
    print("=== ДО: три однополевых индекса (как на боевой таблице) ===")
    before_all = timed(cur, QUERY)
    before_scoped = timed(cur, QUERY_SCOPED, (scope,))
    plan_before = plan(cur, QUERY)
    print(f"  границы по всем камерам    : {before_all[0]:8.1f} мс (макс {before_all[1]:.1f})")
    print(f"    план: {plan_before}")
    print(f"  границы по области 3 камер : {before_scoped[0]:8.1f} мс (макс {before_scoped[1]:.1f})")

    cur.execute(f"CREATE INDEX {INDEX} ON {SEG} (camera_id, started_at, ended_at)")
    cur.execute(f"ANALYZE {SEG}")

    print()
    print("=== ПОСЛЕ: + составной (camera_id, started_at, ended_at) ===")
    after_all = timed(cur, QUERY)
    after_scoped = timed(cur, QUERY_SCOPED, (scope,))
    plan_after = plan(cur, QUERY)
    print(f"  границы по всем камерам    : {after_all[0]:8.1f} мс (макс {after_all[1]:.1f})")
    print(f"    план: {plan_after}")
    print(f"  границы по области 3 камер : {after_scoped[0]:8.1f} мс (макс {after_scoped[1]:.1f})")

    cur.execute("SELECT pg_size_pretty(pg_relation_size(%s))", (INDEX,))
    index_size = cur.fetchone()[0]
    cur.execute("SELECT pg_size_pretty(pg_total_relation_size(%s))", (SEG,))
    table_size = cur.fetchone()[0]

    speedup = before_all[0] / max(after_all[0], 0.001)
    print()
    print("=== ИТОГ ===")
    print(f"  все камеры : {before_all[0]:8.1f} → {after_all[0]:8.1f} мс  ({speedup:.2f}×)")
    print(f"  область    : {before_scoped[0]:8.1f} → {after_scoped[0]:8.1f} мс")
    print(f"  цена индекса: {index_size} на таблицу {table_size}")
    print(f"  план изменился: {'да' if plan_before != plan_after else 'НЕТ — планировщик индекс не выбрал'}")
    print()
    print(f"  §7 «≤ 5 с»: {before_all[0]:.0f} мс, запас {LIMIT_MS / max(before_all[0], 0.001):.0f}×")
    if speedup < 1.1:
        print("  ВЫВОД: составной индекс не окупается — выигрыша нет, диск и "
              "замедление вставки есть. Не добавлять.")

    cur.execute(f"DROP TABLE IF EXISTS {SEG} CASCADE")
    cur.execute(f"DROP TABLE IF EXISTS {CAM} CASCADE")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
