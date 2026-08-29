"""Раскладка каналов аналитики по NUMA-нодам (SPEC §17).

Только stdlib — модуль `cpu_affinity` намеренно не тянет cv2/insightface,
поэтому проверяется в лёгкой джобе CI `worker`, а не пропускается вместе с
тестами, которым нужна модель.

Топология берётся из подставного каталога в стиле `/sys/devices/system/node`
(тот же формат `cpulist`, что отдаёт ядро), а не из машины: у песочницы и
у раннеров CI ровно одна нода, и на настоящей топологии двухсокетный
случай — единственный, ради которого §17 написан, — не проверялся бы
никогда.
"""
import os

import pytest

import cpu_affinity as ca


@pytest.fixture
def two_nodes(tmp_path, monkeypatch):
    """Подставная топология 2× по 8 ядер — форма целевого сервера §20."""
    for node, cpulist in ((0, "0-7"), (1, "8-15")):
        d = tmp_path / f"node{node}"
        d.mkdir()
        (d / "cpulist").write_text(cpulist, encoding="ascii")
    monkeypatch.setattr(ca, "NODE_ROOT", str(tmp_path))
    monkeypatch.setattr(ca, "_allowed_cpus", lambda: set(range(16)))
    monkeypatch.delenv(ca.ENV_MODE, raising=False)
    return ca.numa_nodes()


# --- разбор формата ядра ---------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("0-3", [0, 1, 2, 3]),
    ("0-3,8", [0, 1, 2, 3, 8]),
    ("5", [5]),
    ("0-1,4-5,9", [0, 1, 4, 5, 9]),
    ("", []),
    ("\n", []),
    # Мусор не должен ронять воркер: файл читается с /sys, но на экзотике
    # (не-Linux, подмонтированный вручную sysfs) содержимое произвольно.
    ("0-3,abc,7", [0, 1, 2, 3, 7]),
    ("3-1", []),
])
def test_parse_cpulist(raw, expected):
    assert ca.parse_cpulist(raw) == expected


# --- чтение топологии ------------------------------------------------------

def test_two_nodes_are_read(two_nodes):
    assert two_nodes == [list(range(0, 8)), list(range(8, 16))]


def test_nodes_intersected_with_allowed_cpus(tmp_path, monkeypatch):
    """Под cgroup/`CPUAffinity=` машина видна целиком, а разрешена часть.

    Раскладка по ядрам, которых процессу не дали, кончилась бы `EINVAL` на
    каждой камере — то есть привязка молча не работала бы там, где её как
    раз и настраивают (§26 отдаёт лимиты systemd).
    """
    for node, cpulist in ((0, "0-7"), (1, "8-15")):
        d = tmp_path / f"node{node}"
        d.mkdir()
        (d / "cpulist").write_text(cpulist, encoding="ascii")
    monkeypatch.setattr(ca, "NODE_ROOT", str(tmp_path))
    monkeypatch.setattr(ca, "_allowed_cpus", lambda: {0, 1, 8, 9, 10})
    assert ca.numa_nodes() == [[0, 1], [8, 9, 10]]


def test_node_without_allowed_cpus_disappears(tmp_path, monkeypatch):
    for node, cpulist in ((0, "0-7"), (1, "8-15")):
        d = tmp_path / f"node{node}"
        d.mkdir()
        (d / "cpulist").write_text(cpulist, encoding="ascii")
    monkeypatch.setattr(ca, "NODE_ROOT", str(tmp_path))
    monkeypatch.setattr(ca, "_allowed_cpus", lambda: {0, 1, 2})
    assert ca.numa_nodes() == [[0, 1, 2]]


def test_missing_sysfs_falls_back_to_single_node(monkeypatch):
    monkeypatch.setattr(ca, "NODE_ROOT", "/nonexistent/numa/root")
    monkeypatch.setattr(ca, "_allowed_cpus", lambda: {0, 1, 2, 3})
    assert ca.numa_nodes() == [[0, 1, 2, 3]]


# --- сама раскладка --------------------------------------------------------

def test_single_node_means_no_pinning(monkeypatch):
    """Односокетная машина — привязка не даёт локальности, только мешает.

    Это состояние песочницы, всех раннеров CI и класса «малый объект» §20,
    поэтому оно проверяется отдельно: любой из этих прогонов не должен
    начать привязывать нити.
    """
    monkeypatch.delenv(ca.ENV_MODE, raising=False)
    assert ca.plan([1, 2, 3], nodes=[[0, 1, 2, 3]]) == {}
    assert ca.enabled(nodes=[[0, 1, 2, 3]]) is False


def test_cameras_split_between_nodes_in_blocks(two_nodes):
    """§17: «камеры 1-60 на node 0, 61-120 на node 1» — блоками по id."""
    layout = ca.plan([1, 2, 3, 4], nodes=two_nodes)
    assert layout[1] == list(range(0, 8))
    assert layout[2] == list(range(0, 8))
    assert layout[3] == list(range(8, 16))
    assert layout[4] == list(range(8, 16))


def test_split_is_balanced_for_odd_counts(two_nodes):
    """Остаток размазывается, а не сваливается на последнюю ноду."""
    layout = ca.plan([1, 2, 3, 4, 5], nodes=two_nodes)
    node0 = sum(1 for c in layout.values() if c[0] == 0)
    node1 = len(layout) - node0
    assert {node0, node1} == {3, 2}


def test_layout_covers_every_camera_and_uses_every_node(two_nodes):
    ids = list(range(101, 121))
    layout = ca.plan(ids, nodes=two_nodes)
    assert sorted(layout) == ids
    assert {tuple(c) for c in layout.values()} == {
        tuple(range(0, 8)), tuple(range(8, 16))}


def test_layout_is_stable_for_the_same_input(two_nodes):
    """Перезапуск нити камеры не должен переносить её на другой сокет."""
    ids = [7, 3, 11, 2]
    assert ca.plan(ids, nodes=two_nodes) == ca.plan(reversed(ids), nodes=two_nodes)


def test_duplicate_ids_do_not_shift_the_split(two_nodes):
    assert ca.plan([1, 1, 2, 3, 4], nodes=two_nodes) == ca.plan([1, 2, 3, 4], nodes=two_nodes)


def test_camera_gets_whole_node_not_one_core(two_nodes):
    """Ядро — не единица привязки: пул ORT на камеру бывает больше одного.

    Привязка к одному ядру поставила бы декодер OpenCV и потоки ORT в
    очередь за него (см. ort_threads.MAX_THREADS_PER_CAMERA).
    """
    layout = ca.plan([1, 2], nodes=two_nodes)
    assert all(len(cpus) == 8 for cpus in layout.values())


def test_no_cameras_means_no_layout(two_nodes):
    assert ca.plan([], nodes=two_nodes) == {}


def test_env_off_disables_pinning(two_nodes, monkeypatch):
    monkeypatch.setenv(ca.ENV_MODE, "off")
    assert ca.enabled(nodes=two_nodes) is False
    assert ca.plan([1, 2, 3], nodes=two_nodes) == {}


def test_env_auto_keeps_pinning(two_nodes, monkeypatch):
    monkeypatch.setenv(ca.ENV_MODE, "auto")
    assert ca.enabled(nodes=two_nodes) is True


# --- собственно привязка ---------------------------------------------------

@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"),
                    reason="sched_setaffinity есть только на Linux")
def test_pin_current_thread_affects_only_this_thread():
    """Ключевое свойство, на котором держится весь модуль.

    `os.sched_setaffinity(0, ...)` документирован как «вызывающий
    процесс», но системный вызов Linux применяет его к вызывающей НИТИ.
    Если бы работала документированная формулировка, привязка первой камеры
    утащила бы на её ноду весь воркер — индексацию сегментов, синхронизацию
    слоя записи, ONVIF-опрос. Тест сторожит именно это.
    """
    import threading

    allowed = sorted(os.sched_getaffinity(0))
    if len(allowed) < 2:
        pytest.skip("нужно хотя бы два разрешённых ядра")
    target = {allowed[0]}
    seen: dict[str, set[int]] = {}

    def body():
        seen["pinned"] = ca.pin_current_thread(target)
        seen["thread"] = set(os.sched_getaffinity(0))

    t = threading.Thread(target=body)
    t.start()
    t.join()

    assert seen["pinned"] is True
    assert seen["thread"] == target
    assert set(os.sched_getaffinity(0)) == set(allowed), (
        "привязка нити камеры изменила аффинность всего процесса")


def test_pin_without_cpus_is_a_noop():
    assert ca.pin_current_thread(None) is False
    assert ca.pin_current_thread([]) is False


@pytest.mark.skipif(not hasattr(os, "sched_setaffinity"),
                    reason="sched_setaffinity есть только на Linux")
def test_pin_to_impossible_cpu_does_not_raise():
    """Привязка ускоряет, а не разрешает: её отказ не должен ронять канал."""
    assert ca.pin_current_thread([9999]) is False


# --- сводка для лога и мониторинга ----------------------------------------

def test_describe_reports_layout(two_nodes):
    info = ca.describe([1, 2, 3, 4])
    assert info["numa_nodes"] == 2
    assert info["cpus_per_node"] == [8, 8]
    assert info["pinning"] is True
    assert info["cameras_per_node"] == {0: 2, 1: 2}


def test_describe_on_single_node_says_no_pinning(monkeypatch):
    monkeypatch.setattr(ca, "numa_nodes", lambda: [[0, 1, 2, 3]])
    monkeypatch.delenv(ca.ENV_MODE, raising=False)
    info = ca.describe([1, 2])
    assert info["numa_nodes"] == 1
    assert info["pinning"] is False
    assert info["cameras_per_node"] is None
