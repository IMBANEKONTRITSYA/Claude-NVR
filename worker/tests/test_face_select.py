"""Сопоставление лица со снимка с тем, что сработало на кадре аналитики.

Снимок камера отдаёт на 100-300 мс позже кадра детекции, и в кадре может
быть несколько человек. Ошибка здесь дороже, чем кажется: чужое лицо
попадёт и в карточку персоны, и — что хуже — его эмбеддинг ляжет в событие,
по которому потом идёт поиск по фото.
"""
from face_select import bbox_center, bbox_diagonal, pick_matching_face


def test_single_face_at_expected_place():
    assert pick_matching_face([(100, 100, 200, 200)], (100, 100, 200, 200)) == 0


def test_picks_nearest_of_several_people():
    """В кадре трое — берём того, кто ближе к ожидаемому месту."""
    boxes = [(500, 500, 600, 600), (105, 98, 205, 198), (900, 100, 1000, 200)]
    assert pick_matching_face(boxes, (100, 100, 200, 200)) == 1


def test_small_drift_is_accepted():
    """Человек за 200 мс успевает немного сместиться — это норма, не повод
    отказываться от полноразмерного кропа."""
    assert pick_matching_face([(130, 120, 230, 220)], (100, 100, 200, 200)) == 0


def test_far_face_is_rejected():
    """Единственное лицо, но далеко: нужный человек ушёл, а этот — другой.
    Лучше откатиться на мыльный кроп, чем подписать карточку чужим лицом."""
    assert pick_matching_face([(1200, 800, 1300, 900)], (100, 100, 200, 200)) is None


def test_no_faces_found():
    assert pick_matching_face([], (100, 100, 200, 200)) is None


def test_threshold_scales_with_face_size():
    """Порог — доля от размера рамки, а не абсолютные пиксели: у крупного
    лица вблизи камеры то же смещение в пикселях значит меньше."""
    big = (0, 0, 1000, 1000)
    small = (0, 0, 50, 50)
    moved = (300, 300, 1300, 1300)
    assert pick_matching_face([moved], big) == 0
    assert pick_matching_face([(300, 300, 350, 350)], small) is None


def test_zero_size_expected_bbox_does_not_crash():
    """Вырожденная рамка (нулевая диагональ) не должна давать деление на
    ноль. Лицо ровно в её центре — законное совпадение и принимается;
    удалённое отсекается запасным порогом."""
    assert pick_matching_face([(0, 0, 10, 10)], (5, 5, 5, 5)) == 0
    assert pick_matching_face([(900, 900, 1000, 1000)], (5, 5, 5, 5)) is None


def test_helpers():
    assert bbox_center((0, 0, 100, 200)) == (50.0, 100.0)
    assert bbox_diagonal((0, 0, 3, 4)) == 5.0
