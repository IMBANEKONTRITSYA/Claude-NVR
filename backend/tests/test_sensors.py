"""Какой датчик показывать как «Температуру» (SPEC §9).

§9 перечисляет содержимое системного мониторинга поимённо: «CPU/RAM,
**температура**, место на диске». Величина бралась как первая запись
первой группы в порядке обхода словаря `psutil.sensors_temperatures()`, а
порядок этот задаётся обходом `/sys/class/hwmon` — то есть загрузкой
модулей ядра, а не смыслом.

На целевом сервере §20 (2× Xeon E5-2670, RAID-массив) в словаре лежат
`coretemp` с двумя сокетами, корпусный `acpitz` и `drivetemp`/`nvme` по
каждому диску. «Температура» на «Мониторинге» подписана без уточнений и
читается как температура процессора, поэтому мимо цели можно попасть
дважды: показать диск вместо процессора и показать холодный сокет вместо
горячего.

Проверяется без железа намеренно: `sensors_temperatures()` возвращает
пустой словарь и в песочнице, и на раннере CI, поэтому решение вынесено в
чистую функцию, а сюда приезжают карты датчиков, снятые с реальных
конфигураций.
"""
from collections import namedtuple

from app.services.sensors import cpu_temperature

# Форма записи psutil.sensors_temperatures(): namedtuple с этими полями.
Shw = namedtuple("shwtemp", "label current high critical")


def s(label, current):
    return Shw(label, current, None, None)


# Карта датчиков целевого сервера §20: два сокета Xeon, корпус, массив
# дисков. Порядок ключей — тот, в котором их вернул psutil на такой
# машине: `acpitz` раньше `coretemp`, и именно поэтому прежний код
# показывал корпус.
TWO_SOCKET_XEON = {
    "acpitz": [s("", 38.0)],
    "coretemp": [
        s("Package id 0", 47.0),
        s("Core 0", 45.0), s("Core 1", 46.0),
        s("Package id 1", 92.0),          # второй сокет перегрет
        s("Core 0", 90.0), s("Core 1", 91.0),
    ],
    "drivetemp": [s("Temp1", 41.0), s("Temp1", 43.0)],
    "nvme": [s("Composite", 52.0)],
}


def test_the_cpu_wins_over_the_chassis_and_the_disks():
    """Дефект: показывался `acpitz` — первый в порядке обхода.

    38 °C от корпусного датчика при 92 °C на сокете выглядят
    благополучно, и это ровно тот случай, ради которого §9 просит
    температуру.
    """
    reading = cpu_temperature(TWO_SOCKET_XEON)
    assert reading is not None
    assert reading.celsius == 92.0
    assert reading.source.startswith("coretemp")


def test_the_hotter_socket_wins_not_the_first_one():
    """`entries[0]` — всегда первый сокет, а перегревается тот, что горячее.

    Греется обычно один процессор: ближе к горячему ряду, под отказавшим
    вентилятором. Показав 47 °C первого, система умолчала бы о 92 °C
    второго.
    """
    reading = cpu_temperature(TWO_SOCKET_XEON)
    assert reading.celsius == 92.0
    assert "Package id 1" in reading.source


def test_the_package_wins_over_a_hotter_looking_core():
    """Пакет — то, по чему процессор уходит в throttling.

    Отдельное ядро может быть горячее пакета; пороги вендора выставлены
    на пакет, поэтому при наличии пакетов берутся они.
    """
    reading = cpu_temperature({"coretemp": [s("Core 3", 88.0), s("Package id 0", 70.0)]})
    assert reading.celsius == 70.0
    assert "Package id 0" in reading.source


def test_cores_are_used_when_the_package_sensor_is_absent():
    """На части машин пакетного датчика нет — тогда самое горячее ядро."""
    reading = cpu_temperature({"coretemp": [s("Core 0", 61.0), s("Core 1", 67.0)]})
    assert reading.celsius == 67.0


def test_amd_and_arm_hosts_are_recognised_too():
    """§20 допускает и AMD (Ryzen/EPYC), и малые ARM-платы."""
    assert cpu_temperature({"nvme": [s("Composite", 50.0)],
                            "k10temp": [s("Tctl", 63.0)]}).celsius == 63.0
    assert cpu_temperature({"cpu_thermal": [s("", 55.0)]}).celsius == 55.0


def test_the_chassis_is_used_only_when_there_is_no_cpu_sensor():
    """Корпус хуже процессора, но лучше диска."""
    reading = cpu_temperature({"drivetemp": [s("Temp1", 44.0)],
                               "acpitz": [s("", 36.0)]})
    assert reading.celsius == 36.0
    assert reading.source.startswith("acpitz")


def test_an_unknown_sensor_is_reported_with_its_name():
    """Незнакомая группа годится, но читатель обязан видеть, откуда число.

    Иначе температура диска подписывается как температура системы — та же
    ложь, только другим датчиком.
    """
    reading = cpu_temperature({"nvme": [s("Composite", 52.0)]})
    assert reading.celsius == 52.0
    assert "nvme" in reading.source


def test_no_sensors_means_no_reading_and_not_a_zero():
    """Docker под Windows и раннер CI отдают пустой словарь.

    Ноль здесь показался бы на «Мониторинге» как «0 °C» — показание,
    которого не было.
    """
    assert cpu_temperature({}) is None
    assert cpu_temperature(None) is None


def test_disconnected_sensors_do_not_become_readings():
    """`current` неподключённого датчика приходит нулём или `None`.

    Ноль градусов на работающем сервере — это отсутствующий датчик, и
    попав в выборку, он не испортил бы максимум, но стал бы ответом там,
    где других записей нет.
    """
    assert cpu_temperature({"coretemp": [s("Package id 0", 0.0)]}) is None
    assert cpu_temperature({"coretemp": [s("Package id 0", None)]}) is None
    assert cpu_temperature({"coretemp": [s("Package id 0", 0.0),
                                         s("Package id 1", 61.0)]}).celsius == 61.0


def test_garbage_values_do_not_raise():
    """Показание, которое не приводится к числу, пропускается молча."""
    assert cpu_temperature({"coretemp": [s("Package id 0", "N/A")]}) is None


def test_a_hot_disk_does_not_outrank_an_idle_cpu():
    """Случай, ради которого предпочтение групп и заведено.

    NVMe под нагрузкой уходит за 70 °C, пока процессор простаивает на
    сорока. «Взять самое горячее из всего» показало бы диск и подписало
    его температурой системы; §9 просит температуру машины, и смотрят на
    неё, чтобы поймать перегрев процессора.

    Отдельный тест от `test_the_cpu_wins_over_the_chassis_and_the_disks`:
    там процессор и так самый горячий, поэтому та проверка проходит и на
    коде вовсе без предпочтений.
    """
    reading = cpu_temperature({
        "nvme": [s("Composite", 74.0)],
        "coretemp": [s("Package id 0", 41.0)],
    })
    assert reading.celsius == 41.0
    assert reading.source.startswith("coretemp")
