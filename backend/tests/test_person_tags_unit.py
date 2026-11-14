"""Нормализация тегов персон (SPEC §15) — без БД.

Проверяется не форма вывода, а три обещания модуля, на которых держится
всё остальное: канонизация одинакова на записи и на фильтре, отказ громкий,
слияние не теряет разметку.
"""
import pytest

from app.services.person_tags import (
    MAX_TAG_LEN,
    MAX_TAGS_PER_PERSON,
    TagError,
    merge_tags,
    normalize_tag,
    normalize_tags,
)


def test_tag_canonical_form_is_lowercase_and_whitespace_collapsed():
    # Ровно тот случай, ради которого канонизация и введена: три написания
    # одного тега с объекта, где карточки размечают несколько операторов.
    assert normalize_tag("  Подрядчик ") == "подрядчик"
    assert normalize_tag("ПОДРЯДЧИК") == "подрядчик"
    assert normalize_tag("служба   охраны") == "служба охраны"


def test_normalize_is_idempotent():
    # Повторное сохранение карточки не должно менять её теги: без этого
    # PATCH без правок выглядел бы в журнале аудита как изменение.
    once = normalize_tags(["VIP", " подрядчик "])
    assert normalize_tags(once) == once


def test_duplicates_in_different_case_collapse_to_one():
    assert normalize_tags(["VIP", "vip", "Vip"]) == ["vip"]


def test_empty_entries_are_dropped_not_stored():
    # Следствие ввода «a,,b» — не данные оператора, а разделители.
    assert normalize_tags(["", "   ", "склад"]) == ["склад"]
    assert normalize_tags([]) == []
    assert normalize_tags(None) == []


def test_result_is_sorted_so_equality_is_checkable():
    assert normalize_tags(["склад", "vip", "аренда"]) == sorted(["склад", "vip", "аренда"])


def test_too_long_tag_is_refused_not_truncated():
    # Усечение здесь означало бы, что оператор сохранил один тег, а
    # получил другой, и узнал бы об этом на фильтре.
    with pytest.raises(TagError) as e:
        normalize_tags(["x" * (MAX_TAG_LEN + 1)])
    assert str(MAX_TAG_LEN) in str(e.value)
    # Граница включительно — предел не должен отсекать законный тег.
    assert normalize_tags(["x" * MAX_TAG_LEN]) == ["x" * MAX_TAG_LEN]


def test_too_many_tags_are_refused_not_truncated():
    with pytest.raises(TagError):
        normalize_tags([f"тег{i}" for i in range(MAX_TAGS_PER_PERSON + 1)])
    assert len(normalize_tags([f"тег{i}" for i in range(MAX_TAGS_PER_PERSON)])) == MAX_TAGS_PER_PERSON


def test_limit_counts_unique_tags_not_raw_input():
    # Двадцать одно написание одного тега — это один тег, а не перебор
    # предела: считать надо после дедупликации.
    assert normalize_tags(["vip"] * (MAX_TAGS_PER_PERSON + 1)) == ["vip"]


def test_non_list_input_is_refused():
    with pytest.raises(TagError):
        normalize_tags("vip")  # строка — частая опечатка вызова


def test_merge_keeps_tags_of_both_persons():
    assert merge_tags(["vip"], ["склад"]) == ["vip", "склад"]
    assert merge_tags(["VIP"], ["vip"]) == ["vip"]
    assert merge_tags(None, None) == []


def test_merge_ignores_the_per_person_limit():
    """Слияние не может отказать: источник уже некуда возвращать.

    Обратное поведение (400 на слиянии двух полностью размеченных
    карточек) оставило бы оператора с двумя карточками одного человека и
    без способа их соединить.
    """
    left = [f"л{i}" for i in range(MAX_TAGS_PER_PERSON)]
    right = [f"п{i}" for i in range(MAX_TAGS_PER_PERSON)]
    assert len(merge_tags(left, right)) == 2 * MAX_TAGS_PER_PERSON
