"""Автоконфигурация §16 на production path: реальный Postgres, реальный psutil.

`test_autoconfig_plan.py` рядом проверяет арифметику на выдуманных числах.
Здесь проверяется то, чего та проверка увидеть не может: что эндпоинт
существует, закрыт по матрице прав §18, читает ресурсы настоящей машины,
пишет настройки в настоящую БД и что записанное переживает чтение —
то есть что функция §16 работает, а не только считается.

Фикстура `restore_settings` (возврат таблицы настроек в исходное
состояние) переехала в `conftest.py`: с цикла 32 ею пользуются ещё два
файла, а сторож остатка настроек в `test_zz_suite_leaves_no_residue.py`
роняет прогон, если её забыли.
"""
import pytest


def test_autoconfig_is_admin_only(client, make_user_headers):
    """§18: «Смена профиля производительности» и настройки — только админ.

    Экран планирования показывает ресурсы сервера и позволяет переписать
    профиль одной кнопкой, поэтому обе половины закрыты одинаково.
    """
    for role in ("operator", "viewer"):
        headers = make_user_headers(f"ac_{role}", role)
        assert client.get("/api/system/autoconfig", headers=headers).status_code == 403
        assert client.post("/api/system/autoconfig/apply", headers=headers).status_code == 403
    assert client.get("/api/system/autoconfig").status_code == 401


def test_autoconfig_reports_real_host_resources(client, admin_headers):
    """Ресурсы берутся с машины, а не из констант.

    Сверка идёт с `/api/system/metrics`, который читает тот же psutil
    другим путём: если планирование начнёт отдавать зашитые числа, две
    ветки разъедутся.
    """
    r = client.get("/api/system/autoconfig", headers=admin_headers)
    assert r.status_code == 200, r.text
    res = r.json()["resources"]

    assert res["cores_physical"] >= 1
    assert res["cores_logical"] >= res["cores_physical"]
    assert res["ram_mb"] > 0

    metrics = client.get("/api/system/metrics", headers=admin_headers).json()
    # Тот же хост — та же память с точностью до округления в метриках.
    assert res["ram_mb"] == pytest.approx(metrics["ram_total_mb"], rel=0.01)


def test_plan_is_internally_consistent(client, admin_headers):
    """Итог равен минимуму пределов и назван правильным ресурсом.

    Проверка инварианта, а не чисел: на любой машине, где идёт прогон,
    `recording_max` обязан совпасть с минимальным из трёх пределов, а
    `bound_by` — указать именно на него.
    """
    body = client.get("/api/system/autoconfig", headers=admin_headers).json()
    plan, res = body["plan"], body["resources"]
    rec = plan["recording"]
    assert rec["cameras"] == min(rec["by_cpu"], rec["by_ram"], rec["by_disk"])
    assert rec[f"by_{rec['bound_by']}"] == rec["cameras"]
    assert plan["recording_max"] == rec["cameras"]
    assert plan["profile"] in ("economy", "standard", "maximum")
    assert plan["budget"]["cores_total"] == res["cores_physical"]


def test_plan_counts_physical_cores_not_hyperthreads(client, admin_headers, monkeypatch):
    """Бюджет считается по физическим ядрам, а не по потокам HT/SMT.

    Вилки §16 («0.02-0.04 ядра на камеру») сняты для ядер. Приняв за ядро
    поток, система пообещает вдвое больше камер аналитики, чем сервер
    вывезет: на целевом §20 это 64 потока против 32 ядер.

    Ресурсы подменяются, потому что на машине прогона HT может не быть
    вовсе — в песочнице `cores_physical == cores_logical == 4`, и проверка
    «по факту хоста» не отличила бы одно от другого ни при какой ошибке в
    коде. Ровно так первая версия этой проверки и пропускала подмену
    физических ядер логическими.
    """
    from app.routers import system as system_router

    monkeypatch.setattr(system_router.autoconfig, "detect_resources", lambda _path: {
        "cores_logical": 64, "cores_physical": 32, "ram_mb": 128 * 1024,
        "disk_total_gb": 40960.0, "disk_free_gb": 40960.0,
        "gpu": False, "media_path": "/media",
    })
    body = client.get("/api/system/autoconfig", headers=admin_headers).json()
    assert body["resources"]["cores_logical"] == 64
    assert body["plan"]["budget"]["cores_total"] == 32
    assert body["plan"]["budget"]["cores_usable"] == pytest.approx(25.6)  # 32 × 0.8


def test_what_if_parameters_change_the_answer(client, admin_headers):
    """Битрейт и глубина архива — параметры запроса, а не константы.

    Без этого экран §16 показывал бы один и тот же ответ объекту с
    камерами 2 Мбит/с и объекту с камерами 8 Мбит/с.
    """
    cheap = client.get("/api/system/autoconfig?bitrate_kbps=512&retention_days=3",
                       headers=admin_headers).json()["plan"]
    heavy = client.get("/api/system/autoconfig?bitrate_kbps=8192&retention_days=90",
                       headers=admin_headers).json()["plan"]
    assert cheap["recording"]["by_disk"] > heavy["recording"]["by_disk"]
    assert (cheap["recording"]["gb_per_camera_retention"]
            < heavy["recording"]["gb_per_camera_retention"])


def test_numeric_params_are_bounded(client, admin_headers):
    """Границы у числовых параметров (урок цикла 21: `days=999999999`)."""
    for query in ("?bitrate_kbps=0", "?bitrate_kbps=999999999",
                  "?retention_days=0", "?retention_days=999999999"):
        r = client.get(f"/api/system/autoconfig{query}", headers=admin_headers)
        assert r.status_code == 422, f"{query} → {r.status_code}"


def test_get_does_not_change_anything(client, admin_headers, restore_settings):
    """§16 «предлагает» — значит просмотр не переписывает настройки.

    Иначе открытие мониторинга сбрасывало бы вручную настроенный профиль.

    Профиль сначала уводится в заведомо НЕ рекомендуемый: сравнение «до и
    после» само по себе ложно-зелёное, если система уже стоит на том, что
    предлагает. Первая версия проверки этим и страдала — пишущий GET она
    пропускала, потому что предыдущие тесты файла успевали привести
    настройки к рекомендации, и переписывать было уже нечего.
    """
    recommended = client.get("/api/system/autoconfig",
                             headers=admin_headers).json()["plan"]["profile"]
    other = next(p for p in ("economy", "standard", "maximum") if p != recommended)
    client.post(f"/api/settings/profile/{other}", headers=admin_headers)

    before = client.get("/api/settings", headers=admin_headers).json()
    assert before["performance_profile"] == other
    client.get("/api/system/autoconfig", headers=admin_headers)
    after = client.get("/api/settings", headers=admin_headers).json()
    assert after == before


def test_apply_writes_profile_and_limit_and_survives_reread(
        client, admin_headers, restore_settings):
    """Применение доходит до БД: записанное читается обратно.

    Ключевая половина production path — до неё арифметика могла быть
    сколько угодно верной, а кнопка не работать.

    Как и в проверке выше, профиль предварительно уводится в НЕ
    рекомендуемый: иначе «параметры профиля на месте» может означать
    «они там и лежали», и применение, записывающее один ярлык без
    параметров, прошло бы насквозь.
    """
    plan = client.get("/api/system/autoconfig", headers=admin_headers).json()["plan"]
    other = next(p for p in ("economy", "standard", "maximum") if p != plan["profile"])
    client.post(f"/api/settings/profile/{other}", headers=admin_headers)

    r = client.post("/api/system/autoconfig/apply", headers=admin_headers)
    assert r.status_code == 200, r.text
    applied = r.json()["applied"]
    assert applied["performance_profile"] == plan["profile"]

    saved = client.get("/api/settings", headers=admin_headers).json()
    assert saved["performance_profile"] == plan["profile"]
    assert int(saved["analytics_cameras_max"]) == applied["analytics_cameras_max"]
    # Профиль применяется целиком, а не одним своим именем: воркер читает
    # параметры, а не ярлык (routers/settings.py: apply_profile).
    from app.profiles import PROFILES
    for key, val in PROFILES[plan["profile"]].items():
        assert saved[key] == str(val), key


def test_apply_never_writes_value_rejected_by_settings_schema(
        client, admin_headers, restore_settings):
    """Записанный предел обязан пройти валидацию PUT /api/settings.

    Рекомендация может быть нулевой (слабый сервер), а нижняя граница
    настройки — 1. Если бы применение писало ноль напрямую в таблицу,
    следующее сохранение формы настроек падало бы с 400 на поле, которого
    администратор не касался.
    """
    r = client.post("/api/system/autoconfig/apply", headers=admin_headers)
    written = r.json()["applied"]["analytics_cameras_max"]
    assert 1 <= written <= 16

    echo = client.put("/api/settings", json={"analytics_cameras_max": written},
                      headers=admin_headers)
    assert echo.status_code == 200, echo.text


def test_apply_is_recorded_in_audit_log(client, admin_headers, restore_settings):
    """§10: действие, переписывающее профиль, попадает в журнал аудита.

    Ищется строка, появившаяся ИМЕННО от этого вызова: журнал за собой не
    чистится, и проверка «есть ли где-нибудь в последних 20 строках слово
    „автоконфигурация“» зеленела на записи, оставленной прошлым прогоном
    по той же базе. Снятие ярлыка из `ACTIONS` она пропускала целиком —
    поймано верификацией откатом.
    """
    def newest_id() -> int:
        rows = client.get("/api/audit?limit=1", headers=admin_headers).json()["items"]
        return rows[0]["id"] if rows else 0

    before_id = newest_id()
    client.post("/api/system/autoconfig/apply", headers=admin_headers)

    rows = client.get("/api/audit?limit=20", headers=admin_headers).json()["items"]
    fresh = [r for r in rows if r["id"] > before_id]
    assert fresh, "применение автоконфигурации не оставило в журнале ни одной строки"
    assert any("автоконфигурация" in (r.get("action") or "").lower() for r in fresh), fresh


def test_first_run_flag_clears_after_apply(client, admin_headers, restore_settings):
    """`first_run` — это «предложение ещё не применяли», а не «БД пустая».

    Флаг решает, показывать ли экран самому; если бы он зависел только от
    числа камер, он гас бы после первой камеры, даже если автоконфигурацию
    так и не открывали, — и наоборот, загорался бы снова после удаления
    всех камер на работающем объекте.
    """
    client.post("/api/system/autoconfig/apply", headers=admin_headers)
    after = client.get("/api/system/autoconfig", headers=admin_headers).json()
    assert after["first_run"] is False
    assert after["applied_at"]


def test_current_state_counts_real_cameras(client, admin_headers, make_camera):
    """Блок «сейчас настроено» считает камеры в БД, а не в ответе плана."""
    before = client.get("/api/system/autoconfig", headers=admin_headers).json()["current"]
    make_camera("autoconfig_probe_cam")
    after = client.get("/api/system/autoconfig", headers=admin_headers).json()["current"]
    assert after["cameras_total"] == before["cameras_total"] + 1
