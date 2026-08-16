"""Бенчмарк архива должен мерить ту же таблицу, что есть в приложении.

`perf/bench.py --only archive` создаёт `video_segments`/`face_events`
вручную (DDL строками), а не через `Base.metadata.create_all`: бенчмарк
намеренно не тянет asyncpg/pgvector и не поднимает приложение. Плата за это —
возможность молчаливого расхождения с `models.py`: колонку добавили в
модель, в DDL бенчмарка нет, и замер идёт по более узкой строке, то есть
занижает время.

Тест ловит ровно это. Сам **запрос** сверять не нужно — бенчмарк импортирует
production-функцию `segments_query()`, копии SQL там нет (урок цикла 26).
"""
import re
import sys
from pathlib import Path

import pytest

PERF = Path(__file__).resolve().parents[2] / "perf"


def _bench_module():
    if str(PERF) not in sys.path:
        sys.path.insert(0, str(PERF))
    try:
        import bench  # noqa: F401
    except Exception as e:  # pragma: no cover
        pytest.skip(f"perf/bench.py не импортируется: {e}")
    return bench


def _ddl_columns(ddl: str, table: str) -> set[str]:
    """Имена колонок из CREATE TABLE — первое слово каждой строки тела."""
    body = ddl.split("(", 1)[1]
    cols = set()
    for raw in body.split("\n"):
        token = raw.strip().strip("(),").split(" ")[0].strip(",")
        if token and token.isidentifier() and token.upper() not in {"PRIMARY", "CREATE"}:
            cols.add(token)
    return cols


@pytest.mark.parametrize("table", ["video_segments", "face_events"])
def test_bench_ddl_covers_every_model_column(table):
    from app.models import Base

    bench = _bench_module()
    statements = bench._archive_ddl()
    create = next(s for s in statements
                  if s.lstrip().upper().startswith("CREATE TABLE")
                  and table in s.split("(", 1)[0])

    model_cols = set(Base.metadata.tables[table].columns.keys())
    ddl_cols = _ddl_columns(create, table)

    # embedding (pgvector) в бенчмарке архива не нужен и намеренно опущен:
    # поиск по эмбеддингам меряет отдельная группа facesearch, а хранение
    # 100 000 векторов по 512 float утроило бы время засева.
    ignore = {"embedding"}
    missing = model_cols - ddl_cols - ignore
    assert not missing, (
        f"В DDL бенчмарка нет колонок {table}, которые есть в models.py: "
        f"{sorted(missing)}. Бенчмарк мерит более узкую строку, чем реальная, "
        f"и занижает время поиска по архиву."
    )


def test_bench_ddl_covers_every_model_index():
    """Индексы решают всё: замер на таблице без них не значил бы ничего."""
    from app.models import Base

    bench = _bench_module()
    ddl = "\n".join(bench._archive_ddl())
    indexed = re.findall(r"CREATE INDEX \w+ ON (\w+) \((\w+)\)", ddl)
    have = {(t, c) for t, c in indexed}

    for table in ("video_segments", "face_events"):
        for col in Base.metadata.tables[table].columns:
            if not col.index:
                continue
            assert (table, col.name) in have, (
                f"В models.py на {table}.{col.name} есть index=True, а в DDL "
                f"бенчмарка индекса нет — замер пойдёт по плану, которого в "
                f"приложении не бывает."
            )


def test_bench_uses_production_query_builder():
    """Бенчмарк обязан собирать запрос production-функцией, а не копией SQL.

    Именно копия SQL в бенчмарке позволила §12 двадцать пять циклов
    «укладываться в норматив», ни разу не задев HNSW-индекс.
    """
    source = (PERF / "bench.py").read_text(encoding="utf-8")
    assert "from app.services.archive_query import segments_query" in source, (
        "perf/bench.py больше не импортирует segments_query — если запрос "
        "скопирован в бенчмарк, он разойдётся с приложением молча"
    )


def test_scale_matches_spec():
    """Объём засева — из норматива §7, а не «на глаз».

    Формулировка норматива сменилась вместе с редакцией ТЗ: раньше объём
    выводился из «120 камер × 14 дней» (§1 старой редакции), теперь §7
    задаёт его напрямую — «≤ 5 секунд на объёме до 500 000 сегментов», а
    §1 допускает 12–250+ камер. Проверяется поэтому число сегментов: при
    выводе объёма из числа камер на объекте из 250 камер норматив
    достигался бы вдвое раньше заявленного предела.
    """
    bench = _bench_module()
    assert bench.ARCHIVE_SEGMENTS >= 500_000, "SPEC §7: норматив на 500 000 сегментов"
    assert bench.ARCHIVE_DAYS == 14, "retention по умолчанию — 14 дней"
    # §5: «сегменты 5–10 минут». Берётся нижняя граница — больше строк,
    # то есть худший случай для поиска.
    assert bench.ARCHIVE_SEGMENT_SEC == 300, "SPEC §5: сегменты 5–10 минут"


def test_seeded_volume_reaches_the_norm():
    """Число камер выводится из объёма так, чтобы норматив был достигнут.

    Округление вниз дало бы засев меньше 500 000 строк — замер шёл бы на
    объёме легче норматива и «проходил» бы за счёт этого.
    """
    bench = _bench_module()
    per_camera = bench.ARCHIVE_DAYS * 86400 // bench.ARCHIVE_SEGMENT_SEC
    for target in (1_000, 500_000, 500_001, 1_000_000):
        seeded = bench._archive_cameras(target) * per_camera
        assert seeded >= target, f"засев {seeded} меньше целевых {target} сегментов"
