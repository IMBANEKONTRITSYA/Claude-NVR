"""Автоконфигурация §16: арифметика предложения.

Чистые функции `services/autoconfig.py` проверяются на числах, а не на
железе: сервер из §20 (2× Xeon, 128 ГБ, массив на десятки ТБ) в песочнице
не воспроизводится, а вопрос «что система пообещает на таком сервере» —
единственный, ради которого модуль написан.

Каждая проверка ниже сформулирована как «свойство, которое не должно
сломаться», а не как «функция вернула 132»: числа §16 (0.04 ядра, 100 МБ,
1.5 ядра, 2 ГБ) — это входные константы ТЗ, и тест, сверяющий выход с
результатом той же формулы, проверял бы арифметику Python.
"""
import pytest

from app.services import autoconfig as ac


# Целевой сервер §20 «Большой объект»: 2× Xeon E5-2670 — 32 физических
# ядра, 128 ГБ, массив 40 ТБ. Числа не хардкод количества камер (§22
# запрещает именно его), а описание железа, на котором считается ответ.
TARGET = dict(cores=32, ram_mb=128 * 1024, disk_free_gb=40 * 1024)
# §20 «Малый объект»: Intel N100, 16 ГБ, HDD 2 ТБ.
SMALL = dict(cores=4, ram_mb=16 * 1024, disk_free_gb=2000)


def test_recording_limit_on_target_server_is_bound_by_disk_not_cpu():
    """На сервере §20 предел записи задаёт диск, а не процессор.

    Это не про конкретное число, а про то, что расчёт вообще смотрит на
    диск: remux дёшев по CPU (§16: 0.02-0.04 ядра), и предложение,
    посчитанное по одному процессору, обещало бы больше тысячи камер на
    массиве, которого хватает на сотню.
    """
    plan = ac.plan(**TARGET, retention_days=14, bitrate_kbps=2048)
    rec = plan["recording"]
    assert rec["bound_by"] == "disk"
    assert rec["by_disk"] < rec["by_cpu"]
    assert rec["cameras"] == rec["by_disk"]


def test_recording_limit_falls_when_retention_grows():
    """Глубина архива — прямой множитель предела по диску.

    Проверяется монотонность, а не значение: если расчёт перестанет
    учитывать retention (например, начнёт брать константу §1), число
    камер останется одинаковым на 7 и на 90 сутках.
    """
    week = ac.plan(**TARGET, retention_days=7)["recording_max"]
    quarter = ac.plan(**TARGET, retention_days=90)["recording_max"]
    assert quarter < week
    # Вдесятеро большая глубина — примерно вдесятеро меньше камер.
    assert quarter == pytest.approx(week / 90 * 7, rel=0.05)


def test_analytics_gets_only_what_recording_left():
    """Аналитике достаётся остаток после записи, а не весь сервер (§2).

    Ставится ровно тот случай, ради которого резерв и введён: при большой
    глубине архива камер записи мало, при малой — много, и во втором
    случае аналитике обязано достаться меньше ядер.
    """
    shallow = ac.plan(**TARGET, retention_days=3)   # много камер записи
    deep = ac.plan(**TARGET, retention_days=180)    # мало камер записи
    assert shallow["recording_max"] > deep["recording_max"]
    assert (shallow["budget"]["cores_left_for_analytics"]
            < deep["budget"]["cores_left_for_analytics"])


def test_headroom_is_subtracted_before_anything_else():
    """Запас §19 (20%) не может быть роздан камерам.

    Сравнение идёт с расчётом по «сырым» ядрам: предложение обязано быть
    строго меньше, иначе запас потерян где-то по дороге.
    """
    plan = ac.plan(**TARGET)
    budget = plan["budget"]
    # Числа записаны литералами намеренно. Первая версия проверки считала
    # ожидание как `32 * ac.HEADROOM`, то есть читала ту самую константу,
    # которую и должна стеречь: `HEADROOM = 1.0` проходил её насквозь.
    # Проверка, вычисляющая ожидание по проверяемой константе, стережёт
    # арифметику Python, а не требование §19.
    assert budget["cores_usable"] == pytest.approx(25.6)   # 32 × 0.8
    # Слой приложения вычитается сверх запаса.
    assert budget["cores_for_layers"] == pytest.approx(23.6)  # 25.6 − 2 ядра
    naive = 32 / ac.REC_CORES_PER_CAMERA
    assert plan["recording"]["by_cpu"] < naive


def test_analytics_recommendation_never_exceeds_setting_bounds():
    """Предложение обязано быть применимо (SCHEMA: analytics_cameras_max ≤ 16).

    Иначе «применить» упрётся в валидацию настроек, и единственным
    результатом кнопки станет 400 — то есть функция §16 не работает
    ровно на том железе, где она интереснее всего.
    """
    huge = ac.plan(cores=256, ram_mb=1024 * 1024, disk_free_gb=500 * 1024)
    assert huge["analytics_max"] <= ac.ANALYTICS_SETTING_MAX
    # При этом честный расчёт по ресурсам остаётся виден рядом.
    assert huge["analytics_by_resources"] > ac.ANALYTICS_SETTING_MAX
    assert any("GPU" in w for w in huge["warnings"])


def test_weak_server_gets_zero_analytics_and_says_why():
    """На слабом сервере честный ответ — «аналитику не включать».

    Соблазн выдать «1 камеру» велик (нижняя граница настройки — 1), но
    это обещание, которого железо не выполнит: §16 отводит на камеру
    аналитики до 1.5 ядра, а у N100 после запаса и слоя приложения
    остаётся около одного на всё.
    """
    plan = ac.plan(**SMALL)
    assert plan["analytics_max"] == 0
    assert plan["profile"] == "economy"
    assert any("аналитики" in w for w in plan["warnings"])


def test_gpu_changes_both_capacity_and_profile():
    """С GPU §16 даёт другую вилку CPU (0.1 ядра), и профиль другой."""
    cpu_only = ac.plan(**TARGET, gpu=False)
    with_gpu = ac.plan(**TARGET, gpu=True)
    assert with_gpu["analytics"]["by_cpu"] > cpu_only["analytics"]["by_cpu"]
    assert with_gpu["profile"] == "maximum"


def test_zero_and_negative_resources_do_not_produce_negative_cameras():
    """Пустая машина даёт ноль камер, а не отрицательное число.

    Слой приложения (`APP_CORES`, `APP_RAM_MB`) вычитается безусловно, и
    на машине слабее него разность отрицательна. Без явного пола это
    уехало бы в интерфейс как «-17 камер».
    """
    plan = ac.plan(cores=1, ram_mb=512, disk_free_gb=0)
    assert plan["recording_max"] == 0
    assert plan["analytics_max"] == 0
    assert plan["budget"]["cores_for_layers"] >= 0
    assert plan["budget"]["ram_mb_for_layers"] >= 0

    # Пол проверяется и на самих функциях ёмкости, а не только через
    # `plan()`: тот сам обрезает бюджет по нулю раньше, поэтому до пола
    # внутри `_floor_nonneg` отрицательное значение через него не доходит
    # — снятие пола проверка через `plan()` не замечала вовсе. Обе функции
    # публичные, вызываются напрямую и обязаны держать контракт сами.
    assert ac.recording_capacity(-5, -100, -10, 2048, 14)["cameras"] == 0
    assert ac.analytics_capacity(-5, -100)["cameras"] == 0


def test_zero_bitrate_means_disk_limit_unknown_not_unlimited():
    """Битрейт 0 не должен превращаться в «камер сколько угодно».

    Деление на ноль здесь не падает, а даёт соблазн пропустить диск из
    сравнения — и тогда ответом станет предел по CPU, то есть тысяча
    камер на пустом массиве.
    """
    plan = ac.plan(**TARGET, bitrate_kbps=0)
    assert plan["recording"]["by_disk"] == 0
    assert plan["recording_max"] == 0


def test_profile_thresholds_follow_available_cores_per_camera():
    """Профиль выбирается по ядрам на камеру, а не по числу камер."""
    assert ac.recommend_profile(cores_available=30, analytics_cameras=2) == "maximum"
    assert ac.recommend_profile(cores_available=4, analytics_cameras=2) == "standard"
    assert ac.recommend_profile(cores_available=2, analytics_cameras=2) == "economy"
    # Ровно на границе — включительно (иначе 3.0 ядра дают economy).
    exact = ac.PROFILE_CORES_MAXIMUM * 2
    assert ac.recommend_profile(cores_available=exact, analytics_cameras=2) == "maximum"
    exact_std = ac.PROFILE_CORES_STANDARD * 2
    assert ac.recommend_profile(cores_available=exact_std, analytics_cameras=2) == "standard"


def test_no_analytics_cameras_still_yields_a_profile():
    """Ноль камер аналитики не должен ронять выбор профиля делением на ноль."""
    assert ac.recommend_profile(cores_available=0, analytics_cameras=0) == "economy"


def test_storage_formula_matches_spec_and_storage_module():
    """Формула §21 не должна разъехаться со второй своей копией.

    `services/storage.py` держит ту же константу для прогноза уже
    работающего архива. Разъезд копий означал бы, что планирование и
    прогноз показывают разные цифры на одних и тех же камерах.
    """
    from app.services import storage

    assert ac.GB_PER_DAY_PER_MBPS == storage.GB_PER_DAY_PER_MBPS == 10.8
    assert ac.gb_per_camera_day(2048) == pytest.approx(
        storage.nominal_gb_per_day(2048), rel=1e-9)


def test_constants_are_the_upper_edge_of_spec_ranges():
    """Константы §16 — верх вилок, а не низ и не середина.

    Ошибка в эту сторону даёт заниженное предложение (видно сразу,
    ничего не ломает); в обратную — завышенное, которое обнаружится
    через сутки по пропущенным сегментам. Тест стережёт направление.
    """
    assert ac.REC_CORES_PER_CAMERA == 0.04        # §16: 0.02-0.04
    assert ac.REC_RAM_MB_PER_CAMERA == 100        # §16: 50-100 MB
    assert ac.ANALYTICS_CORES_PER_CAMERA == 1.5   # §16: 0.5-1.5
    assert ac.ANALYTICS_RAM_MB_PER_CAMERA == 2048  # §16: 500 MB - 2 GB
    assert ac.ANALYTICS_CORES_PER_CAMERA_GPU == 0.1  # §16: ~0.1 ядра с GPU
    assert ac.HEADROOM == 0.8                     # §19: запас 20%
