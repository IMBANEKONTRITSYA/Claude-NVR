"""Юнит-тесты разбора и нормировки центроида (services/person_centroid.py).

Интеграционная часть — в `test_integration_person_merge_centroid.py`: там
слияние через API на настоящем Postgres. Здесь — чистые функции, у которых
есть ровно те краевые случаи, из-за которых пересчёт мог бы испортить
карточку вместо того, чтобы её починить.
"""
import math

import pytest

from app.services.person_centroid import (
    format_vector,
    normalize,
    parse_vector,
)


def test_parse_vector_reads_pgvector_text():
    assert parse_vector("[1,2,3]") == [1.0, 2.0, 3.0]
    assert parse_vector("[-0.5,0.25]") == [-0.5, 0.25]


def test_parse_vector_on_nothing_returns_none():
    """`avg()` по персоне без событий отдаёт NULL, а не нулевой вектор.

    Если бы разбор превращал это в `[]` или в нули, пересчёт записал бы
    карточке пустой центроид — то есть сделал бы её неузнаваемой вовсе,
    тогда как прежний центроид ещё работал.
    """
    assert parse_vector(None) is None
    assert parse_vector("") is None
    assert parse_vector("[]") is None
    assert parse_vector("[ ]") is None


def test_normalize_gives_unit_length():
    unit = normalize([3.0, 4.0])
    assert unit == pytest.approx([0.6, 0.8])
    assert math.sqrt(sum(x * x for x in unit)) == pytest.approx(1.0)


def test_normalize_keeps_direction_of_already_unit_vector():
    vec = [0.6, -0.8]
    assert normalize(vec) == pytest.approx(vec)


def test_normalize_refuses_zero_vector():
    """Нулевой центроид — деление на ноль и карточка, «похожая на всё».

    Средний вектор обращается в ноль на противоположных эмбеддингах; это
    маловероятно, но отказ здесь дешевле, чем NaN в колонке, по которой
    строится HNSW-индекс.
    """
    assert normalize([0.0, 0.0, 0.0]) is None
    assert normalize([1e-12, -1e-12]) is None


def test_format_vector_roundtrips_through_parse():
    """Запись в колонку и чтение обратно не должны терять точность.

    Через `repr`, а не через `f"{x:.4f}"`: округление до четырёх знаков на
    512 координатах сдвигает нормированный вектор настолько, что
    косинусное расстояние до собственных эмбеддингов карточки перестаёт
    совпадать с посчитанным в воркере.
    """
    vec = normalize([0.1234567901234, -0.98765432109, 0.5])
    again = parse_vector(format_vector(vec))
    assert again == vec
