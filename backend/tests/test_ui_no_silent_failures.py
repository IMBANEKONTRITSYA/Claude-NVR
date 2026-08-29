"""Мутирующий вызов API из интерфейса не должен отказывать молча.

`req()` в `frontend/src/api.ts` бросает `Error` на любой `!res.ok`. Значит
`await api.somethingPut(...)` без обработки отказа даёт одно из двух:

* оператор нажал кнопку, запрос отказал — и в интерфейсе не появилось
  ничего (обработчик прервался на await, toast об успехе не выполнился);
* поле неуправляемое (`defaultValue`) — введённый текст остался на экране
  и выглядит сохранённым, хотя не сохранён.

Цикл 27 нашёл пять таких мест: сохранение зон ROI (§11) и четыре действия
над карточкой персоны (§8) — переименование, слияние, галочка watchlist и
заметки. Соседние обработчики в тех же файлах (`enhance`, `remove`,
удаление снимка) отказ обрабатывали — то есть это был недосмотр, а не
осознанное решение.

Проверка написана на класс целиком, а не на пять исправленных строк
(урок цикла 21: регрессионная проверка должна покрывать свой класс, иначе
следующий такой же вызов проедет мимо неё). Тест живёт в backend/tests
потому, что фронтенд-тестраннера в проекте нет вовсе — см. carryover про
§16 «Тесты: pytest, Jest, locust».
"""
import re
from pathlib import Path

FRONTEND_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"

# Имена методов api.*, которые меняют состояние на сервере. Читать их из
# api.ts целиком нельзя: там же лежат геттеры, у которых молчаливый отказ
# не теряет действие оператора (страница просто останется пустой).
MUTATING = re.compile(
    r"\bapi\.(\w*(?:Put|Post|Delete|Patch|Add|Create|Update|Merge|Enhance|Apply|Bulk|Import)\w*)\s*\(",
    re.IGNORECASE,
)

# Сколько строк назад/вперёд считать «тем же обработчиком». Обработчики в
# проекте короткие (десяток строк), 25/15 берут их с запасом.
LOOKBEHIND, LOOKAHEAD = 25, 15


def _unguarded_calls() -> list[str]:
    found = []
    for path in sorted(FRONTEND_SRC.rglob("*.tsx")) + sorted(FRONTEND_SRC.rglob("*.ts")):
        if path.name == "api.ts":
            continue  # сам клиент: тут вызовы и определяются, а не совершаются
        lines = path.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            m = MUTATING.search(line)
            if not m:
                continue
            if ".catch(" in line:
                continue  # промис-стиль: отказ обработан по месту
            before = "\n".join(lines[max(0, i - LOOKBEHIND):i + 1])
            after = "\n".join(lines[i:i + LOOKAHEAD])
            # «try открыт и ещё не закрыт» — последний try идёт после
            # последнего catch в окне позади, а catch есть впереди.
            last_try = before.rfind("try")
            last_catch = before.rfind("catch")
            guarded = last_try > last_catch and "catch" in after
            if not guarded:
                rel = path.relative_to(FRONTEND_SRC.parents[1])
                found.append(f"{rel}:{i + 1}: api.{m.group(1)}() без try/catch — "
                             f"{line.strip()[:80]}")
    return found


def test_no_mutating_api_call_without_error_handling():
    unguarded = _unguarded_calls()
    assert not unguarded, (
        "Мутирующий вызов API без обработки отказа — оператор не увидит ошибку:\n"
        + "\n".join(unguarded)
    )


def test_scanner_actually_detects_an_unguarded_call():
    """Позитивный контроль: без него тест выше зеленел бы и на сломанном
    регулярном выражении, которое не находит вообще ничего (урок цикла 21 —
    в каждом наборе нужен тест, который стережёт саму проверку)."""
    sample = [
        "const save = async () => {",
        "  await api.camRoiPut(camId, norm);",
        "  toast('ok');",
        "};",
    ]
    hits = [l for l in sample if MUTATING.search(l) and ".catch(" not in l]
    assert hits, "регулярное выражение перестало находить мутирующие вызовы"


def test_roi_page_offers_only_analytics_cameras():
    """SPEC §11: зоны детекции задаются только для камер в режиме analytics.

    Бэкенд отдаёт 400 на попытку сохранить ROI для record_only. Пока список
    камер на странице не был отфильтрован, оператор узнавал об этом только
    после того, как нарисовал полигон, — а record_only по §3 режим по
    умолчанию, то есть на объекте из 120 камер отказом заканчивалось
    подавляющее большинство попыток.
    """
    roi = (FRONTEND_SRC / "pages" / "ROI.tsx").read_text(encoding="utf-8")
    assert 'mode === "analytics"' in roi, (
        "ROI.tsx снова показывает камеры всех режимов — сохранение зон для "
        "record_only отказывает на бэкенде (cameras.py: put_roi)"
    )
