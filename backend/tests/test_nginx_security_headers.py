"""Регрессия: `add_header` в nginx не накапливается по уровням.

Директивы `add_header` наследуются с предыдущего уровня конфигурации
**только если на текущем уровне нет ни одного собственного `add_header`**
(документированное поведение nginx, не баг). У `location /hls/` их два
своих — `Cache-Control` и `Access-Control-Allow-Origin`, — и из-за этого
все заголовки безопасности, объявленные уровнем выше, для видеопотока
пропадали.

Замерено на настоящем nginx 1.24 до исправления (цикл 21):

    GET /                    → X-Frame-Options, X-Content-Type-Options,
                               Referrer-Policy, Content-Security-Policy
    GET /hls/cam1/index.m3u8 → Cache-Control, Access-Control-Allow-Origin
                               — и больше ничего

То есть `.m3u8`-плейлисты и `.ts`-сегменты, проксируемые из MediaMTX, шли
без `nosniff` (а их содержимое приходит от камеры), без `X-Frame-Options`
и, на HTTPS-хосте, без HSTS — при том, что ТЗ 13 требует «настройки
безопасности (заголовки)» на reverse proxy.

Проверка статическая и не требует ни nginx, ни Docker: она разбирает те же
файлы, что попадают в образ. Тот же приём, что в
`test_dockerfile_modules.py` — инвариант, который в песочнице без
Docker-демона иначе не проверить (одиннадцатый цикл подряд).
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FRONTEND = ROOT / "frontend"
SNIPPET_NAME = "nginx-security-headers.conf"
SNIPPET_INCLUDE = f"include /etc/nginx/snippets/{SNIPPET_NAME};"

REQUIRED_HEADERS = [
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Content-Security-Policy",
    "Strict-Transport-Security",
]


def _locations_with_own_add_header(text: str) -> list[str]:
    """Заголовки location-блоков, объявляющих собственный `add_header`.

    Разбор по фигурным скобкам с учётом вложенности: `location` в этом
    конфиге не вложены друг в друга, но полагаться на это не стоит.
    """
    found = []
    for m in re.finditer(r"^\s*(location\s[^\{]*)\{", text, re.MULTILINE):
        header = m.group(1).strip()
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        if re.search(r"^\s*add_header\s", body, re.MULTILINE):
            found.append(header)
    return found


def test_security_headers_snippet_declares_every_required_header():
    """Страховка от того, что проверки ниже начнут стеречь пустой файл."""
    snippet = (FRONTEND / SNIPPET_NAME).read_text(encoding="utf-8")
    declared = set(re.findall(r"^\s*add_header\s+(\S+)", snippet, re.MULTILINE))
    missing = [h for h in REQUIRED_HEADERS if h not in declared]
    assert not missing, f"{SNIPPET_NAME} не объявляет заголовки: {missing}"


def test_every_location_with_own_add_header_reincludes_the_snippet():
    """Каждый location со своими add_header обязан включить сниппет заново.

    Это и есть то самое правило nginx: у такого location унаследованные
    add_header не работают, и «унаследовать и дополнить» можно только
    повторным include.
    """
    text = (FRONTEND / "nginx-locations.conf").read_text(encoding="utf-8")
    locations = _locations_with_own_add_header(text)
    assert locations, (
        "разбор конфига не нашёл ни одного location с собственным add_header — "
        "проверка сломана и ничего не стережёт"
    )

    offenders = []
    for m in re.finditer(r"^\s*(location\s[^\{]*)\{", text, re.MULTILINE):
        header = m.group(1).strip()
        if header not in locations:
            continue
        depth, i = 1, m.end()
        while i < len(text) and depth:
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
            i += 1
        body = text[m.end():i - 1]
        if SNIPPET_INCLUDE not in body:
            offenders.append(header)

    assert not offenders, (
        f"эти location объявляют свои add_header и не включают {SNIPPET_NAME}: "
        f"{offenders}. add_header в nginx не накапливается по уровням — все "
        "заголовки безопасности с уровня server{} для них молча пропадут "
        f"(добавьте `{SNIPPET_INCLUDE}` внутрь блока)"
    )


def test_server_level_includes_the_snippet_once():
    """Общий уровень тоже идёт через сниппет, а не своей копией заголовков."""
    text = (FRONTEND / "nginx-locations.conf").read_text(encoding="utf-8")
    # Верхний уровень файла — всё до первой директивы `location ... {`.
    # Именно директивы, а не слова: в комментариях выше по файлу слово
    # «location» встречается, и разрез по нему обрезал бы верхний уровень
    # раньше самого include (проверка падала бы на здоровом конфиге).
    first = re.search(r"^\s*location\s[^\{]*\{", text, re.MULTILINE)
    top_level = text[:first.start()] if first else text
    assert SNIPPET_INCLUDE in top_level, (
        f"уровень server{{}} должен включать {SNIPPET_NAME}, а не дублировать "
        "заголовки — иначе две копии разъедутся"
    )
    assert not re.search(r"^\s*add_header\s", top_level, re.MULTILINE), (
        "заголовки на верхнем уровне должны идти только из сниппета"
    )


def test_hsts_is_conditional_on_https():
    """HSTS не должен уходить по обычному HTTP.

    RFC 6797 §8.1 обязывает браузер игнорировать заголовок, пришедший по
    небезопасному транспорту, — но раз сниппет один на оба server{},
    значение должно приходить из `map $https`, а не быть константой.
    """
    snippet = (FRONTEND / SNIPPET_NAME).read_text(encoding="utf-8")
    m = re.search(r"^\s*add_header\s+Strict-Transport-Security\s+(\S+)", snippet, re.MULTILINE)
    assert m, "в сниппете нет add_header Strict-Transport-Security"
    assert m.group(1).startswith("$"), (
        f"значение HSTS — константа {m.group(1)!r}; сниппет включается и в "
        "HTTP-server{}, где заголовок стал бы мёртвым кодом. Нужна переменная "
        "из map $https (см. nginx.conf)"
    )

    conf = (FRONTEND / "nginx.conf").read_text(encoding="utf-8")
    var = m.group(1).lstrip("$")
    assert re.search(rf"map\s+\$https\s+\${var}\s*\{{", conf), (
        f"nginx.conf не объявляет map $https ${var} — переменная будет пустой "
        "на обоих server{}, и HSTS не уйдёт даже по HTTPS"
    )


def test_dockerfile_copies_the_snippet_into_the_image():
    """Сниппет должен попасть в образ: без него nginx не стартует вообще
    (`include` несуществующего файла — фатальная ошибка конфигурации)."""
    dockerfile = (FRONTEND / "Dockerfile").read_text(encoding="utf-8")
    assert SNIPPET_NAME in dockerfile, (
        f"frontend/Dockerfile не копирует {SNIPPET_NAME} в /etc/nginx/snippets/ — "
        "контейнер фронтенда упадёт на старте с 'open() ... failed'"
    )
