"""SPEC §2, §15: источник кадров аналитики — основной поток либо субпоток
с разрешением не ниже 640×480.

Набор проверяет решение, а не рендер: сам выбор потока живёт в чистой
функции ровно для того, чтобы его можно было проверить без cv2 и RTSP.
"""

import pytest

from analytics_source import (MIN_ANALYTICS_HEIGHT, MIN_ANALYTICS_WIDTH,
                              NO_SUB, SUB_BELOW_FLOOR, SUB_BELOW_FLOOR_NO_MAIN,
                              SUB_MEETS_FLOOR, SUB_RESOLUTION_UNKNOWN,
                              choose_analytics_source, describe, meets_floor,
                              resolution_known)

MAIN = "rtsp://cam/main"
SUB = "rtsp://cam/sub"


class TestMeetsFloor:
    @pytest.mark.parametrize("w,h", [
        (640, 480),      # ровно порог — «не ниже» включает равенство
        (704, 576),      # D1 PAL, типовой субпоток по §1
        (1280, 720),
        (1920, 1080),
    ])
    def test_at_or_above_floor(self, w, h):
        assert meets_floor(w, h) is True

    @pytest.mark.parametrize("w,h", [
        (352, 288),      # CIF — §1 называет его типовым субпотоком
        (640, 360),      # то, что комментарии worker.py считали кадром аналитики
        (639, 480),      # не хватает одного пикселя по ширине
        (640, 479),      # ...и по высоте
        (320, 240),      # QVGA
    ])
    def test_below_floor(self, w, h):
        assert meets_floor(w, h) is False

    @pytest.mark.parametrize("w,h", [(0, 0), (None, None), (640, 0), (0, 480),
                                     (-1, -1), (None, 480)])
    def test_unknown_is_not_a_pass(self, w, h):
        """Неизмеренное разрешение не считается прошедшим порог."""
        assert meets_floor(w, h) is False
        assert resolution_known(w, h) is False


class TestChoice:
    def test_no_sub_url_uses_main(self):
        d = choose_analytics_source(MAIN, None)
        assert d["url"] == MAIN
        assert d["stream"] == "main"
        assert d["reason"] == NO_SUB

    def test_sub_above_floor_is_kept(self):
        """Штатный случай: субпоток дотягивает — на нём и работаем."""
        d = choose_analytics_source(MAIN, SUB, 704, 576)
        assert d["url"] == SUB
        assert d["stream"] == "sub"
        assert d["reason"] == SUB_MEETS_FLOOR
        assert (d["width"], d["height"]) == (704, 576)

    def test_cif_sub_falls_back_to_main(self):
        """Главный дефект цикла 53: CIF-субпоток уходил в детектор молча.

        §1 называет CIF типовым субпотоком, а автообнаружение ONVIF (§3)
        кладёт в поле субпотока самый маленький профиль камеры — то есть
        этот путь проходит штатно заведённая камера, а не экзотика.
        """
        d = choose_analytics_source(MAIN, SUB, 352, 288)
        assert d["url"] == MAIN, "субпоток ниже порога §15 не может быть источником кадров"
        assert d["stream"] == "main"
        assert d["reason"] == SUB_BELOW_FLOOR
        # Числа субпотока сохраняются: именно из-за них принято решение,
        # и без них дежурный не поймёт, что чинить на камере.
        assert (d["width"], d["height"]) == (352, 288)

    def test_640x360_sub_falls_back(self):
        """Кадр, который worker.py много лет считал типовым, порог не проходит."""
        d = choose_analytics_source(MAIN, SUB, 640, 360)
        assert d["stream"] == "main"
        assert d["reason"] == SUB_BELOW_FLOOR

    def test_unknown_resolution_keeps_sub(self):
        """Не измерили — не трогаем: уход на основной поток стоит CPU."""
        d = choose_analytics_source(MAIN, SUB, None, None)
        assert d["url"] == SUB
        assert d["stream"] == "sub"
        assert d["reason"] == SUB_RESOLUTION_UNKNOWN

    def test_below_floor_without_main_stays_on_sub(self):
        """Уходить некуда — работаем как есть, но причина видна."""
        d = choose_analytics_source("", SUB, 352, 288)
        assert d["url"] == SUB
        assert d["reason"] == SUB_BELOW_FLOOR_NO_MAIN

    def test_below_floor_when_main_equals_sub(self):
        """Одинаковые адреса — переключение бессмысленно, а не «сделано»."""
        d = choose_analytics_source(SUB, SUB, 352, 288)
        assert d["url"] == SUB
        assert d["reason"] == SUB_BELOW_FLOOR_NO_MAIN

    def test_floor_constants_match_spec(self):
        """Порог берётся из SPEC §2/§15, а не из головы."""
        assert (MIN_ANALYTICS_WIDTH, MIN_ANALYTICS_HEIGHT) == (640, 480)


class TestDescribe:
    @pytest.mark.parametrize("decision", [
        choose_analytics_source(MAIN, None),
        choose_analytics_source(MAIN, SUB, 704, 576),
        choose_analytics_source(MAIN, SUB, 352, 288),
        choose_analytics_source("", SUB, 352, 288),
        choose_analytics_source(MAIN, SUB, None, None),
    ])
    def test_every_reason_has_a_note(self, decision):
        """У каждой причины есть текст: пустая ячейка в §9 ничего не сообщает."""
        assert describe(decision).strip()

    def test_note_carries_the_numbers(self):
        note = describe(choose_analytics_source(MAIN, SUB, 352, 288))
        assert "352" in note and "288" in note
        assert "640" in note and "480" in note
