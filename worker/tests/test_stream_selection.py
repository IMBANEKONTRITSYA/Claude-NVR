"""Выбор основного потока и субпотока, пересчёт рамки лица между кадрами.

Регрессия, найденная пользователем на живой установке: после массового
добавления лица в карточках персон оказались нечитаемыми, и апскейл не
помогал. Причин две, обе здесь.

1. select_stream_profiles: первая версия сортировала профили по разрешению
   ВСЕГДА, давая профилям без разрешения вес -1. На прошивках, которые не
   возвращают VideoEncoderConfiguration в GetProfiles (таких много), «самым
   большим» оказывался последний профиль списка — то есть субпоток. Он
   записывался в камеру как основной поток, субпоток оставался пустым, и
   воркер и анализировал, и писал архив с 640×360.

2. Даже при верном выборе потоков снимок лица резался из кадра аналитики,
   то есть из субпотока. Лицо в нём — несколько десятков пикселей, и
   апскейлу восстанавливать нечего. Отсюда scale_bbox: рамка, найденная на
   субпотоке, переносится в координаты полноразмерного снимка.
"""
import pytest

pytest.importorskip("defusedxml", reason="ONVIF-клиент требует defusedxml")

from onvif_client import scale_bbox, select_stream_profiles  # noqa: E402


def _p(token, name, w=None, h=None):
    return {"token": token, "name": name, "width": w, "height": h}


# --- Выбор потоков ----------------------------------------------------------

def test_picks_by_resolution_when_known():
    main, sub = select_stream_profiles([
        _p("p1", "MainStream", 1920, 1080),
        _p("p2", "SubStream", 640, 360),
    ])
    assert main["name"] == "MainStream"
    assert sub["name"] == "SubStream"


def test_resolution_wins_over_list_order():
    """Порядок профилей прошивки не гарантируют: субпоток может идти первым."""
    main, sub = select_stream_profiles([
        _p("p1", "SubStream", 640, 360),
        _p("p2", "MainStream", 2560, 1440),
    ])
    assert main["name"] == "MainStream"
    assert sub["name"] == "SubStream"


def test_falls_back_to_order_when_resolution_unknown():
    """Ровно случай пользователя: камера вернула только token и Name.

    До фикса здесь основным становился SubStream — последний элемент после
    сортировки, — а субпоток вообще не заполнялся.
    """
    main, sub = select_stream_profiles([
        _p("p1", "MainStream"),
        _p("p2", "SubStream"),
    ])
    assert main["name"] == "MainStream", "основной поток — первый профиль по конвенции ONVIF"
    assert sub["name"] == "SubStream"


def test_partial_resolution_also_falls_back_to_order():
    """Разрешение известно только у одного профиля — сравнивать не с чем,
    порядок надёжнее половинчатой сортировки."""
    main, sub = select_stream_profiles([
        _p("p1", "MainStream", 1920, 1080),
        _p("p2", "SubStream"),
    ])
    assert main["name"] == "MainStream"
    assert sub["name"] == "SubStream"


def test_single_profile_has_no_substream():
    main, sub = select_stream_profiles([_p("p1", "OnlyStream", 1920, 1080)])
    assert main["name"] == "OnlyStream"
    assert sub is None


def test_single_profile_without_resolution():
    main, sub = select_stream_profiles([_p("p1", "OnlyStream")])
    assert main["name"] == "OnlyStream"
    assert sub is None


def test_no_profiles():
    assert select_stream_profiles([]) == (None, None)


def test_three_profiles_pick_extremes():
    main, sub = select_stream_profiles([
        _p("p1", "Mid", 1280, 720),
        _p("p2", "Main", 1920, 1080),
        _p("p3", "Sub", 640, 360),
    ])
    assert main["name"] == "Main"
    assert sub["name"] == "Sub"


# --- Пересчёт рамки между потоками ------------------------------------------

def test_scales_bbox_from_substream_to_full_frame():
    """Лицо в середине субпотока 640×360 должно оказаться в середине
    полноразмерного кадра 1920×1080."""
    assert scale_bbox((320, 180, 400, 260), (640, 360), (1920, 1080)) == (960, 540, 1200, 780)


def test_scales_independently_per_axis():
    """Соотношение сторон основного потока и субпотока совпадает не всегда —
    16:9 против 4:3 встречается на практике, и общий коэффициент увёл бы
    рамку мимо лица."""
    assert scale_bbox((100, 100, 200, 200), (640, 480), (1920, 1080)) == (300, 225, 600, 450)


def test_clamps_to_frame_bounds():
    """Рамка у самого края не должна выйти за пределы кадра — иначе кроп
    получится пустым."""
    x1, y1, x2, y2 = scale_bbox((-10, -10, 700, 400), (640, 360), (1920, 1080))
    assert (x1, y1) == (0, 0)
    assert x2 <= 1920 and y2 <= 1080


def test_identity_when_sizes_match():
    assert scale_bbox((10, 20, 30, 40), (1920, 1080), (1920, 1080)) == (10, 20, 30, 40)


def test_zero_source_size_is_not_a_crash():
    """Защита от деления на ноль, если размеры кадра почему-то нулевые."""
    assert scale_bbox((10, 20, 30, 40), (0, 0), (1920, 1080)) == (10, 20, 30, 40)
