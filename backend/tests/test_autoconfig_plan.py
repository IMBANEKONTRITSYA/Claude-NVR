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
    """Предложение обязано быть применимо: оно не выходит за границу схемы.

    Иначе «применить» упрётся в валидацию настроек, и единственным
    результатом кнопки станет 400 — то есть функция §16 не работает
    ровно на том железе, где она интереснее всего.

    Верхняя граница проверяется через саму константу и через SCHEMA
    роутера: до цикла 47 это были две независимые копии числа 16, и
    расходились бы они молча.
    """
    from app.routers.settings import SCHEMA

    assert SCHEMA["analytics_cameras_max"][2] == ac.ANALYTICS_SETTING_MAX

    huge = ac.plan(cores=4096, ram_mb=16 * 1024 * 1024, disk_free_gb=5000 * 1024)
    assert huge["analytics_max"] <= ac.ANALYTICS_SETTING_MAX
    # При этом честный расчёт по ресурсам остаётся виден рядом.
    assert huge["analytics_by_resources"] > ac.ANALYTICS_SETTING_MAX
    assert any("§1" in w for w in huge["warnings"])


def test_target_server_gets_more_than_the_old_hardcoded_sixteen():
    """Сервер из §20 («Большой объект», 2× CPU) получает то, что вывозит.

    Это и есть регрессия, ради которой границу меняли: до цикла 47
    предложение упиралось в 16 — число, выведенное из «2-3 ядер на
    аналитику» удалённой редакции ТЗ и записанное в схему валидации,
    хотя §22 прямо запрещает хардкодить количество аналитики.

    Ожидание сформулировано как «больше прежнего потолка», а не «ровно
    29»: 29 — следствие вилок §16, и привязываться к нему значило бы
    ломать тест при первой же их правке по замеру.
    """
    plan = ac.plan(cores=64, ram_mb=128 * 1024, disk_free_gb=40_000)
    assert plan["analytics_by_resources"] > 16
    assert plan["analytics_max"] == plan["analytics_by_resources"]


def test_gpu_advice_appears_where_spec_20_puts_it():
    """Совет про ускоритель привязан к отметке §20, а не к потолку схемы.

    Прежнее предупреждение говорило «выше 16 нужен GPU» — то есть про
    границу настройки, а не про железо, и на сервере, где по ресурсам
    проходит 29 каналов, администратор не узнавал главного: §20
    рекомендует ускоритель уже с 20.
    """
    many = ac.plan(cores=64, ram_mb=128 * 1024, disk_free_gb=40_000)
    assert many["analytics_by_resources"] > ac.GPU_RECOMMENDED_ABOVE
    assert any("ускоритель" in w for w in many["warnings"])

    # С ускорителем совет не нужен и не показывается.
    with_gpu = ac.plan(cores=64, ram_mb=128 * 1024, disk_free_gb=40_000, gpu=True)
    assert not any("ускоритель" in w for w in with_gpu["warnings"])

    # Ниже отметки §20 — тоже не показывается.
    few = ac.plan(cores=12, ram_mb=32 * 1024, disk_free_gb=8_000)
    assert few["analytics_by_resources"] <= ac.GPU_RECOMMENDED_ABOVE
    assert not any("ускоритель" in w for w in few["warnings"])


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


def test_cpu_constants_are_the_upper_edge_of_spec_ranges():
    """Вилки §16 по ПРОЦЕССОРУ берутся сверху, а не снизу и не по середине.

    Ошибка в эту сторону даёт заниженное предложение (видно сразу,
    ничего не ломает); в обратную — завышенное, которое обнаружится
    через сутки по пропущенным сегментам. Тест стережёт направление.
    """
    assert ac.REC_CORES_PER_CAMERA == 0.04        # §16: 0.02-0.04
    assert ac.ANALYTICS_CORES_PER_CAMERA == 1.5   # §16: 0.5-1.5
    assert ac.ANALYTICS_CORES_PER_CAMERA_GPU == 0.1  # §16: ~0.1 ядра с GPU
    assert ac.HEADROOM == 0.8                     # §19: запас 20%


def test_memory_is_modelled_as_a_constant_plus_a_per_camera_cost():
    """Память — «постоянная часть + N × камеру», а не «N × камеру».

    Вилки §16 по памяти (50-100 МБ и 500 МБ-2 ГБ на камеру) писались под
    раскладку «процесс на камеру». Реализовано иначе: один MediaMTX на
    все камеры записи, одна модель на все каналы аналитики. Деление всей
    памяти на удельный расход в такой раскладке заставляет платить
    постоянную часть заново за каждую камеру — на сервере из §20 с 32 ГБ
    это давало 4 канала аналитики там, где по процессору проходит 29, и
    советовало администратору докупать память вместо процессора.

    Проверяется форма модели, а не конкретные мегабайты: числа приходят
    из замера (`perf/bench_ram_scaling.py`) и меняются с каждым уточнением.
    """
    # Постоянная часть существует и вычитается: удвоение памяти даёт
    # БОЛЬШЕ чем удвоение числа камер, потому что константу платят один раз.
    small = ac.analytics_capacity(cores=1000, ram_mb=8192)["by_ram"]
    big = ac.analytics_capacity(cores=1000, ram_mb=16384)["by_ram"]
    assert big > small * 2

    # Памяти меньше постоянной части — ноль камер, а не отрицательное число.
    assert ac.analytics_capacity(
        cores=1000, ram_mb=ac.ANALYTICS_RAM_CONSTANT_MB - 1)["by_ram"] == 0

    # Стоимость камеры строго ниже вилки §16: вилка писалась под другую
    # раскладку, и совпадение с ней означало бы, что правку откатили.
    assert ac.ANALYTICS_RAM_MB_PER_CAMERA < 500
    assert ac.REC_RAM_MB_PER_CAMERA < 50


def test_analytics_is_bound_by_cpu_not_memory_on_the_target_server():
    """На сервере из §20 аналитику ограничивает процессор, а не память.

    Это следствие того, что модель общая: память слоя почти не растёт с
    числом каналов (замер цикла 47 — 56 МБ на канал против 434 МБ
    постоянной части), а процессор растёт линейно. До цикла 47 расчёт
    говорил обратное и на 32-64 ГБ показывал «упирается в оперативную
    память» — то есть советовал докупать не то.
    """
    for ram_gb in (32, 64, 128):
        plan = ac.plan(cores=64, ram_mb=ram_gb * 1024, disk_free_gb=40_000)
        assert plan["analytics"]["bound_by"] == "cpu", ram_gb


def test_render_node_alone_is_not_an_inference_accelerator(monkeypatch):
    """`/dev/dri` — не признак ускорителя для инференса.

    Ответ «да» переключает расчёт с 1.5 ядра на камеру на 0.1 (§16), то
    есть завышает предел аналитики в пятнадцать раз. До цикла 47 его
    давал любой узел рендера — то есть любая встроенная графика на
    сервере, где сборка ONNX Runtime CPU-шная и считать на iGPU нечем.
    Ошибка шла ровно в ту сторону, которую докстринг модуля обещает не
    допускать («по верхней границе система обещает меньше»).

    Проверяется поведением, а не чтением исходника: подменяется и
    `shutil.which`, и `os.path.exists`.
    """
    import os as _os
    import shutil as _shutil

    monkeypatch.setattr(_shutil, "which", lambda name: None)
    monkeypatch.setattr(_os.path, "exists", lambda path: True)
    assert ac.detect_gpu() is False

    # NVIDIA с nvidia-smi по-прежнему считается ускорителем.
    monkeypatch.setattr(_shutil, "which",
                        lambda name: "/usr/bin/nvidia-smi" if name == "nvidia-smi" else None)
    assert ac.detect_gpu() is True
