"""Все CSV-выгрузки помечены BOM (SPEC §3, §8, §10).

**Что именно сторожат эти тесты.** У CSV нет отказа «неверная кодировка»:
файл без BOM открывается в Excel всегда и молча — просто кириллица в нём
выглядит как «Ð’Ñ€ÐµÐ¼Ñ». Поэтому расхождение прожило до 64-го цикла и
было видно только глазами в самом Excel: выгрузка камер §3 BOM писала,
отчёты §8 и журнал аудита §10 — нет. Ни один существующий тест этого не
замечал, потому что все они читают ответ как текст (`r.text`, где httpx
уже снял BOM) или сравнивают разобранные строки.

Отсюда правило набора: **проверять байты ответа, а не разобранный CSV.**
Разбор одинаково успешен и с BOM, и без него — то есть тест по разбору
зелен при обоих исходах и контракт не сторожит.

Четвёртая проверка — вложение планировщика: §8 требует «автоматическую
отправку по расписанию (email)», и уходящий почтой отчёт открывают тем же
Excel, что и скачанный.
"""
import io

import pytest

from app.services import csv_export

BOM_BYTES = b"\xef\xbb\xbf"


def test_encode_marks_the_text():
    assert csv_export.encode("Время,Камера").startswith(BOM_BYTES)


def test_encode_is_idempotent():
    """Повторная пометка не даёт второго BOM.

    Не гипотетический случай: выгрузка камер §3 помечала результат сама
    (`.encode("utf-8-sig")`), и переход на общий вызов без этой защиты дал
    бы «﻿﻿name,…». Первый столбец заголовка перестал бы
    совпадать с именем поля, и круговой обход «выгрузил → загрузил»
    сломался бы на импорте.
    """
    once = csv_export.encode("name,location")
    twice = csv_export.encode(once.decode("utf-8"))
    assert once == twice
    assert twice.count(BOM_BYTES) == 1


def test_reports_csv_is_marked(client, admin_headers):
    """§8: отчёт скачивается с BOM — все его заголовки кириллические."""
    r = client.get("/api/reports/appearances.csv", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.content.startswith(BOM_BYTES), (
        f"отчёт §8 отдан без BOM: {r.content[:20]!r} — в Excel заголовки "
        f"будут прочитаны как cp1251"
    )


def test_audit_csv_is_marked(client, admin_headers):
    """§10: журнал аудита — та же кодировка, что у отчётов."""
    r = client.get("/api/audit/export.csv", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.content.startswith(BOM_BYTES), (
        f"журнал §10 отдан без BOM: {r.content[:20]!r}"
    )


def test_camera_export_csv_is_marked(client, admin_headers):
    """§3: выгрузка камер — единственная, что была помечена и до правки.

    Проверка остаётся: смысл общего модуля в том, что все три выгрузки
    ходят одним путём, и «камеры» не должны потерять BOM при переносе.
    """
    r = client.get("/api/cameras/export", params={"format": "csv"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.content.startswith(BOM_BYTES)


def test_camera_export_json_is_not_marked(client, admin_headers):
    """§3: JSON помечать нельзя — BOM ломает строгий разбор.

    Тест держит границу правки: общий кодировщик применяется к CSV и
    только к нему.
    """
    r = client.get("/api/cameras/export", params={"format": "json"}, headers=admin_headers)
    assert r.status_code == 200, r.text
    assert not r.content.startswith(BOM_BYTES)
    import json
    json.loads(r.content.decode("utf-8"))  # разбор без снятия BOM обязан пройти


def test_marked_csv_still_round_trips_through_import(client, admin_headers):
    """§3: помеченный файл по-прежнему принимается импортом.

    Это и есть цена вопроса: выгрузка камер — не только отчёт, но и вход
    импорта. `camera_config.parse_file` декодирует `utf-8-sig`, то есть
    BOM снимает; тест проверяет это на настоящем ответе эндпоинта, а не
    на собранной в тесте строке.
    """
    exported = client.get("/api/cameras/export", params={"format": "csv"},
                          headers=admin_headers)
    assert exported.status_code == 200
    assert exported.content.startswith(BOM_BYTES)

    from app.services import camera_config
    rows = camera_config.parse_file(exported.content)
    # Заголовок разобран как имя поля, а не как «﻿name».
    assert isinstance(rows, list)
    if rows:
        assert "name" in rows[0], f"BOM не снят при разборе: ключи {list(rows[0])[:3]}"


# Вложение, уходящее почтой по расписанию (§8, «автоматическая отправка»),
# проверяется там, где живут фикстуры SMTP, —
# `test_integration_report_schedules.py::test_send_now_delivers_csv_attachment`.
