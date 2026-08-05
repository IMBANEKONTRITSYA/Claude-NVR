"""Первый запуск на Windows: генерация .env и поведение start.bat при сбое.

Регрессия, найденная на реальной установке пользователя: `start.bat` печатал
ParserError из `scripts/generate_env.ps1`, молча копировал `.env.example` в
`.env` и запускал стек, после чего вход в систему не работал.

Цепочка была такая:
1. `generate_env.ps1` лежал в UTF-8 БЕЗ BOM. Windows PowerShell 5.1 (её и
   вызывает start.bat командой `powershell`) читает .ps1 без BOM в ANSI
   системы — на русской Windows CP1251. Буква 'ф' (UTF-8 D1 84) при этом
   декодируется в U+201E („), а этот символ парсер PowerShell считает
   закрывающей двойной кавычкой — строковый литерал рвётся, разбор падает.
2. start.bat откатывался на `.env.example`, где все три секрета — публичные
   значения из репозитория.
3. `insecure_secret_problems()` их отвергает, `main.py:lifespan` бросает
   RuntimeError, а с `restart: unless-stopped` контейнер backend уходит в
   бесконечный CrashLoop.
4. Пользователь видел «ошибку входа», хотя пароль вводил правильный —
   обрабатывать запрос было некому.

Windows и PowerShell в CI нет, поэтому проверяется не выполнение скрипта, а
инварианты его исходника, каждый из которых соответствует одному звену
цепочки выше. Это ровно те инварианты, нарушение которых и вызвало сбой.
"""
import pathlib
import re

import pytest

from app.config import INSECURE_ADMIN_PASSWORDS, INSECURE_RTSP_ENCRYPTION_KEYS, INSECURE_SECRET_KEYS, Settings, insecure_secret_problems

ROOT = pathlib.Path(__file__).resolve().parents[2]
PS1 = ROOT / "scripts" / "generate_env.ps1"
START_BAT = ROOT / "start.bat"
ENV_EXAMPLE = ROOT / ".env.example"

UTF8_BOM = b"\xef\xbb\xbf"

# Символы, которые парсер PowerShell трактует как разделители строковых
# литералов. Именно попадание одного из них в текст и валило разбор.
PS_QUOTE_CHARS = {
    0x0022, 0x201C, 0x201D, 0x201E,  # двойные
    0x0027, 0x2018, 0x2019, 0x201B,  # одинарные
}


def test_ps1_has_utf8_bom():
    """Корневая причина сбоя. Без BOM PowerShell 5.1 читает файл как CP1251."""
    assert PS1.read_bytes().startswith(UTF8_BOM), (
        "scripts/generate_env.ps1 обязан быть в UTF-8 С BOM — иначе Windows "
        "PowerShell 5.1 прочитает его в ANSI-кодировке системы и кириллица "
        "развалит разбор скрипта"
    )


def test_ps1_is_safe_under_ansi_misread():
    """Настоящий инвариант безопасности, шире одного лишь BOM.

    Скрипт не должен разваливаться у пользователя, и закрыть это можно двумя
    равноправными способами: либо BOM (тогда PowerShell 5.1 прочитает файл
    как UTF-8), либо отсутствие в тексте символов, которые при ошибочном
    чтении в CP1251 дают кавычку-разделитель (то есть сообщения на ASCII).
    Тест требует выполнения хотя бы одного и потому переживает смену
    стратегии фикса, но ловит ситуацию, когда не выполнено ни одного —
    ровно то состояние, в котором файл был у пользователя.
    """
    raw = PS1.read_bytes()
    has_bom = raw.startswith(UTF8_BOM)

    body = raw[len(UTF8_BOM):] if has_bom else raw
    offenders = []
    for lineno, line in enumerate(body.split(b"\n"), start=1):
        try:
            mojibake = line.decode("cp1251")
        except UnicodeDecodeError:
            continue
        dangerous = {c for c in mojibake if ord(c) in PS_QUOTE_CHARS and c != '"' and c != "'"}
        if dangerous:
            offenders.append((lineno, "".join(sorted(dangerous))))

    assert has_bom or not offenders, (
        "scripts/generate_env.ps1 не защищён ни BOM, ни ASCII-текстом: строки "
        f"{offenders} при чтении в CP1251 дают кавычки-разделители PowerShell "
        "и развалят разбор скрипта на русской Windows"
    )


def test_cp1251_misread_really_produces_a_quote_delimiter():
    """Проверка самого механизма, а не файла: это тот факт, на котором держатся
    тесты выше. Буква 'ф' в UTF-8 — D1 84; байт 0x84 в CP1251 — U+201E („),
    который PowerShell считает закрывающей двойной кавычкой.

    Именно так рвалась строка 54 старой версии: в выводе пользователя ошибка
    указывала на колонки 122 и 123, что точно совпадает с позицией ')' и конца
    строки после обрыва литерала.
    """
    original = '(сохранён в .env, при желании смените в интерфейсе после входа)'
    mojibake = original.encode("utf-8").decode("cp1251")

    assert "„" in mojibake, "ожидался U+201E из буквы 'ф'"
    assert ord("„") in PS_QUOTE_CHARS

    # Воспроизводим колонки из отчёта пользователя.
    line = 'Write-Host "' + mojibake + '"'
    assert line.index(")") + 1 == 122
    assert len(line) == 123


def _ps1_code() -> str:
    """Исходник .ps1 без строк-комментариев.

    Тесты ниже ищут в скрипте признаки конкретных ошибок. Сами эти признаки
    упомянуты в пояснительных комментариях («раньше здесь стоял ::Fill()»),
    поэтому поиск по всему файлу давал бы ложное срабатывание на объяснении
    вместо кода.
    """
    lines = PS1.read_text(encoding="utf-8-sig").splitlines()
    return "\n".join(l for l in lines if not l.lstrip().startswith("#"))


def _bat_code() -> str:
    """Исходник .bat без комментариев (rem) и без текста, выводимого echo.

    В подсказке пользователю start.bat печатает ровно те команды, которые
    ему нужно выполнить руками (`copy .env.example .env`) — это текст для
    человека, а не действие скрипта, и путать их тест не должен.
    """
    lines = START_BAT.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for l in lines:
        s = l.lstrip().lower()
        if s.startswith("rem ") or s.startswith("echo "):
            continue
        out.append(l)
    return "\n".join(out)


def test_ps1_uses_no_dotnet_core_only_apis():
    """Второй блокер, независимый от кодировки: `RandomNumberGenerator::Fill`
    появился в .NET Core 2.1, а Windows PowerShell 5.1 работает на .NET
    Framework 4.x, где его нет. Починка одного лишь BOM оставила бы скрипт
    нерабочим — он упал бы уже на генерации ключей."""
    src = _ps1_code()
    assert "::Fill(" not in src, (
        "RandomNumberGenerator::Fill() недоступен в .NET Framework 4.x "
        "(Windows PowerShell 5.1). Нужен ::Create() + GetBytes()"
    )
    assert "RandomNumberGenerator]::Create()" in src, (
        "ожидается кросс-версионный способ получения случайных байт"
    )


def test_ps1_reads_template_as_utf8():
    """`.env.example` — UTF-8 без BOM с кириллическими комментариями.
    `Get-Content` в PS 5.1 по умолчанию читает в ANSI, и комментарии уехали
    бы в .env мозаикой."""
    src = _ps1_code()
    assert re.search(r"Get-Content[^\n]*-Encoding UTF8", src), (
        "чтение .env.example должно быть явно в UTF8"
    )


def test_ps1_writes_env_without_bom():
    """`Set-Content -Encoding utf8` в PS 5.1 добавляет BOM (в PS 7 — нет).
    BOM приклеился бы к первому ключу .env как \\ufeffPOSTGRES_USER."""
    src = _ps1_code()
    assert "UTF8Encoding($false)" in src, "запись .env должна быть UTF-8 без BOM"
    assert not re.search(r"Set-Content[^\n]*-Encoding utf8", src), (
        "Set-Content -Encoding utf8 в PowerShell 5.1 пишет BOM — использовать "
        "[System.IO.File]::WriteAllText с UTF8Encoding($false)"
    )


def test_template_defaults_are_exactly_what_backend_rejects():
    """Инвариант, на котором держится решение start.bat не откатываться на
    шаблон: `.env.example` не может служить рабочим .env. Если кто-то
    когда-нибудь сделает значения шаблона валидными, этот тест упадёт и
    напомнит пересмотреть логику start.bat."""
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    values = dict(
        line.split("=", 1)
        for line in text.splitlines()
        if "=" in line and not line.strip().startswith("#")
    )
    assert values["SECRET_KEY"] in INSECURE_SECRET_KEYS
    assert values["RTSP_ENCRYPTION_KEY"] in INSECURE_RTSP_ENCRYPTION_KEYS
    assert values["ADMIN_PASSWORD"].lower() in INSECURE_ADMIN_PASSWORDS

    # И, для полноты, что бэкенд действительно откажется стартовать.
    s = Settings(
        SECRET_KEY=values["SECRET_KEY"],
        RTSP_ENCRYPTION_KEY=values["RTSP_ENCRYPTION_KEY"],
        ADMIN_PASSWORD=values["ADMIN_PASSWORD"],
        ALLOW_INSECURE_DEFAULT_SECRETS=False,
    )
    problems = insecure_secret_problems(s)
    assert len(problems) == 3, f"ожидались все три проблемы, получено: {problems}"


def test_start_bat_does_not_copy_template_as_env():
    """Собственно дефект: молчаливый откат на шаблон гарантировал мёртвый
    backend и «ошибку входа» вместо честного сообщения о сбое."""
    src = _bat_code()
    assert not re.search(r"copy\s+\.env\.example\s+\.env", src, re.I), (
        "start.bat не должен молча копировать .env.example в .env — бэкенд "
        "на этих значениях не стартует, пользователь увидит только ошибку входа"
    )


def test_start_bat_fails_loudly_without_env():
    """После попытки генерации отсутствие .env обязано останавливать запуск,
    а не продолжать сборку контейнеров."""
    src = _bat_code()
    assert "if not exist .env goto" in src, (
        "нужна явная проверка, что .env появился после генерации"
    )
    assert re.search(r":no_env\b", src), "нужна ветка обработки отсутствующего .env"


@pytest.mark.parametrize(
    "marker",
    [
        "SECRET_KEY=please-change-me-to-a-long-random-string",
        "RTSP_ENCRYPTION_KEY=ZmFjZXdhdGNoLWRldi1rZXktMzJieXRlcy1iYXNlNjQ=",
        "ADMIN_PASSWORD=admin",
    ],
)
def test_start_bat_detects_already_broken_env(marker):
    """У пострадавших от старой версии .env уже создан шаблоном, и условие
    `if not exist .env` больше никогда не сработает — сама по себе новая
    версия их бы не спасла. Поэтому start.bat проверяет содержимое."""
    src = START_BAT.read_text(encoding="utf-8", errors="replace")
    assert marker in src, (
        f"start.bat должен распознавать оставшийся от старого отката .env "
        f"по маркеру {marker}"
    )
    assert re.search(r":insecure_env\b", src), "нужна ветка для такого .env"


def test_start_bat_pins_working_directory():
    """Все проверки .env в start.bat идут по относительным путям, поэтому
    рабочий каталог обязан быть корнем репозитория. «Запуск от имени
    администратора» стартует .bat в C:\\Windows\\system32 — без cd /d "%~dp0"
    новые проверки смотрели бы не туда и ругались бы на исправную установку."""
    src = _bat_code()
    assert 'cd /d "%~dp0"' in src, (
        "start.bat должен переходить в свой каталог до работы с .env"
    )
    # Переход обязан быть до первой проверки .env, иначе он бесполезен.
    assert src.index('cd /d "%~dp0"') < src.index("if not exist .env")


def test_gitattributes_forces_crlf_for_batch_files():
    """start.bat перешёл на метки goto в конце файла, а cmd.exe ищет их
    посимвольным сканированием и документированно сбоит на файлах с одними
    LF. В репозитории файлы лежат с LF, поэтому окончания строк на checkout
    должен фиксировать .gitattributes — иначе клонирование с
    core.autocrlf=false отдаёт .bat, у которого может не найтись :no_env."""
    ga = ROOT / ".gitattributes"
    assert ga.exists(), ".gitattributes нужен, чтобы .bat приезжал с CRLF"
    text = ga.read_text(encoding="utf-8")
    assert re.search(r"^\*\.bat\s+text\s+eol=crlf", text, re.M), (
        "*.bat должен принудительно получать CRLF на checkout"
    )


def test_start_bat_is_ascii_only():
    """cmd.exe читает .bat в OEM-кодировке (CP866 на русской Windows), а файл
    хранится в UTF-8 — кириллица в .bat вывелась бы мозаикой. Та же природа,
    что и у корневого бага, поэтому фиксируем инвариант явно."""
    raw = START_BAT.read_bytes()
    non_ascii = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    assert not non_ascii, (
        f"start.bat должен быть чистым ASCII, найдены байты на позициях "
        f"{[i for i, _ in non_ascii[:5]]}"
    )
