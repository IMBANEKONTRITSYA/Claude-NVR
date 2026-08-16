"""Нагрузочный сценарий описывает масштаб действующего ТЗ (SPEC §16 + §26).

§16 требует, чтобы сценарий был «пересмотрен под новый масштаб (см. §26)»,
а §26 задаёт его буквально: «120 камер в режиме записи (remux) + N камер
аналитики (по умолчанию 2)».

До цикла 27 `loadtest/locustfile.py` реализовывал сценарий **удалённой**
редакции — `TARGET_CAMERAS = 16`, все камеры без указания режима (то есть
все `record_only`), — и ссылался на «SPEC.md §14», которым в действующей
редакции называется совсем другой раздел. Пункт §16 при этом числился
закрытым с цикла 13: сценарий существовал и работал, просто проверял
масштаб, которого в ТЗ уже нет.

Тест читает исходник, а не импортирует модуль: `locust` не входит в
зависимости бэкенда и в CI-джобе backend его нет.
"""
import re
from pathlib import Path

LOADTEST = Path(__file__).resolve().parents[2] / "loadtest"
LOCUSTFILE = LOADTEST / "locustfile.py"


def _const(name: str) -> int:
    """Значение по умолчанию у `NAME = int(os.environ.get(..., "N"))`."""
    src = LOCUSTFILE.read_text(encoding="utf-8")
    m = re.search(rf'^{name}\s*=\s*int\(os\.environ\.get\([^,]+,\s*"(\d+)"\)\)',
                  src, re.MULTILINE)
    assert m, f"в locustfile.py не найдена константа {name}"
    return int(m.group(1))


def test_scenario_covers_all_120_recording_cameras():
    """SPEC §26: «120 камер в режиме записи (remux)»."""
    assert _const("TARGET_CAMERAS") == 120, (
        "Нагрузочный сценарий рассчитан не на 120 камер. SPEC §16 требует "
        "пересмотра сценария под масштаб §26; 16 — число удалённой редакции ТЗ."
    )


def test_scenario_keeps_analytics_cameras_at_spec_default():
    """SPEC §1: аналитика «только на N выбранных камерах (по умолчанию 2)»."""
    assert _const("ANALYTICS_CAMERAS") == 2, (
        "Число камер analytics в сценарии разошлось с §1 (по умолчанию 2). "
        "Сценарий, где в аналитике все 120, нагружает не ту систему: по §24 "
        "детекция на всех 120 камерах без GPU явно вне рамок версии."
    )


def test_scenario_assigns_camera_modes():
    """Камеры должны заводиться с явным режимом, а не «как получится».

    Без `mode` в payload все 120 камер уезжают в `record_only` (режим по
    умолчанию §3), и сценарий не задевает ни одного запроса, специфичного
    для аналитики.
    """
    src = LOCUSTFILE.read_text(encoding="utf-8")
    assert '"mode": mode' in src, (
        "камеры в сценарии создаются без указания режима — тогда камер "
        "analytics не появится вовсе (§3: record_only по умолчанию)"
    )
    assert '"analytics"' in src and '"record_only"' in src


def test_grid_page_matches_spec_limit():
    """SPEC §4: «грид до 16 камер», «для 120 камер — выбор группы/страницы».

    Оператор опрашивает снепшоты своей страницы, а не всех 120 подряд —
    иначе нагрузка размазывается по всему парку ровным слоем, чего в
    интерфейсе не происходит.
    """
    src = LOCUSTFILE.read_text(encoding="utf-8")
    m = re.search(r"^GRID_PAGE_SIZE\s*=\s*(\d+)", src, re.MULTILINE)
    assert m, "в сценарии нет GRID_PAGE_SIZE"
    assert int(m.group(1)) == 16, "SPEC §4 ограничивает грид 16 камерами"


def test_cites_the_section_it_actually_implements():
    """Сценарий обязан ссылаться на §16, а не на §14 удалённой редакции.

    Проверяется утверждение «что реализуем», а не наличие строки «§14»
    где угодно: упоминание старой ссылки в историческом абзаце («до цикла
    27 файл ссылался на …») полезно и запрещать его нечего. Отсюда
    позитивная формулировка — ищем действующую ссылку, а не отсутствие
    прежней.
    """
    for path in (LOCUSTFILE, LOADTEST / "README.md"):
        text = path.read_text(encoding="utf-8")
        assert re.search(r"SPEC §16", text), (
            f"{path.name} не называет §16 разделом, который реализует "
            "(нагрузочное тестирование в действующей редакции — §16, "
            "а §14 — отказоустойчивость и мониторинг)"
        )
        assert "§26" in text, (
            f"{path.name} не ссылается на §26, где задан масштаб сценария"
        )


def test_scenario_is_not_described_as_sixteen_cameras():
    """«Сценарий на 16 камер» — формулировка удалённой редакции ТЗ.

    Ищется именно описание сценария, а не число 16 вообще: 16 остаётся
    легитимным как размер грида (§4) и как число виртуальных операторов.
    """
    for path in (LOCUSTFILE, LOADTEST / "README.md"):
        text = path.read_text(encoding="utf-8")
        for phrase in ("сценарий на 16 камер", "сценария на 16 камер",
                       "работе с 16 камерами", "сетки на 16"):
            assert phrase not in text, (
                f"{path.name} описывает сценарий как «{phrase}» — масштаб "
                "удалённой редакции; по §26 это 120 камер записи + 2 аналитики"
            )


def test_scenario_touches_recording_layer_endpoints():
    """Слой записи — главное, что масштабируется до 120 (§14, §9, §21).

    `/api/system/record-layer` считает два агрегата по `video_segments` за
    сутки (на 120 камерах это ~34 560 строк) и потому обязан быть в
    сценарии: именно его стоимость растёт вместе с парком камер.
    """
    src = LOCUSTFILE.read_text(encoding="utf-8")
    for endpoint in ("/api/system/record-layer", "/api/system/storage"):
        assert endpoint in src, (
            f"сценарий не нагружает {endpoint} — запрос, стоимость которого "
            "растёт с числом камер и объёмом архива"
        )
