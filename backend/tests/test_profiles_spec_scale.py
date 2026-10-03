"""Профили производительности описаны в терминах действующего ТЗ (SPEC §19).

Два требования §19, которые проверяются здесь:

1. «Профили производительности сохранены (Экономный/Стандартный/
   Максимальный), **применяются только к слою аналитики**; слой записи
   всегда в режиме remux». Значит ни один ключ профиля не имеет права
   управлять записью — иначе смена профиля в админке способна повлиять на
   запись 120 камер, а §2 требует независимости слоёв.

2. Подписи профилей не должны описывать железо удалённой редакции ТЗ.
   До цикла 27 администратору показывались строки «Экономный — слабое
   железо (Intel N100), 4–8 камер» и «Максимальный — Core i5+ / GPU,
   12–16 камер». В Scale Edition это вводит в заблуждение дважды: числа
   читаются как относящиеся к 120 камерам записи (а профиль касается
   только камер analytics, по умолчанию 2), а «Core i5+ / GPU» — как
   несовместимость с целевым сервером 2× E5-2670, у которого GPU нет
   вовсе (§23).
"""
import asyncio
import re

import pytest

from app.profiles import DEFAULT_PROFILE, PROFILE_KEYS, PROFILES, profile_settings

# Ключи слоя записи: их профиль трогать не может ни при каких условиях.
# Список не выдуман — это настройки, которые читает менеджер записи.
RECORDING_KEYS = {
    "record_segment_min",
    "retention_days",
    "disk_warn_pct",
    "disk_crit_pct",
    "analytics_cameras_max",
}

# Марки железа и числа камер из удалённой редакции ТЗ.
DELETED_EDITION = re.compile(
    r"N100|Celeron|Core i\d|Ryzen|GTX|\b4–8\b|\b8–12\b|\b12–16\b|\b16 камер",
    re.IGNORECASE,
)


def _profiles_payload() -> dict:
    """Ответ /api/settings/profiles — ровно то, что получает браузер."""
    from app.routers.settings import list_profiles

    return asyncio.run(list_profiles(_=None))


def test_profiles_are_the_three_from_spec():
    assert set(PROFILES) == {"economy", "standard", "maximum"}, (
        "SPEC §19 называет ровно три профиля: Экономный/Стандартный/Максимальный"
    )
    assert DEFAULT_PROFILE == "standard"


def test_no_profile_key_touches_recording_layer():
    """SPEC §19 + §2: слой записи всегда remux и от профиля не зависит."""
    leaked = set(PROFILE_KEYS) & RECORDING_KEYS
    assert not leaked, (
        f"Профиль управляет настройками слоя записи: {sorted(leaked)}. "
        "По SPEC §19 профиль применяется только к слою аналитики, а по §2 "
        "смена профиля не имеет права влиять на запись 120 камер."
    )
    # То же для фактически записываемых значений, а не только объявленных
    # ключей: profile_settings() — это то, что уезжает в таблицу settings.
    for name in PROFILES:
        written = set(profile_settings(name))
        assert not (written & RECORDING_KEYS), (
            f"Профиль {name} пишет настройку слоя записи: "
            f"{sorted(written & RECORDING_KEYS)}"
        )


def test_profile_titles_describe_behaviour_not_deleted_edition_hardware():
    """Подписи из /api/settings/profiles — то, что читает администратор."""
    payload = _profiles_payload()
    titles = payload["titles"]
    assert set(titles) == set(PROFILES)

    for name, text in titles.items():
        found = DELETED_EDITION.search(text)
        assert not found, (
            f"Подпись профиля «{name}» описывает железо/масштаб удалённой "
            f"редакции ТЗ ({found.group(0)!r}): {text!r}. Целевой сервер — "
            "2× E5-2670 без GPU (§23), а профиль касается камер analytics "
            "(по умолчанию 2), а не 120 камер записи."
        )


def test_profiles_response_states_analytics_only():
    """§19 прямо сказано администратору, а не только в комментарии кода."""
    payload = _profiles_payload()
    note = payload.get("note", "")
    assert "аналитик" in note.lower(), (
        "Ответ /api/settings/profiles не сообщает, что профиль применяется "
        "только к слою аналитики (SPEC §19) — администратор объекта на 120 "
        "камер разумно предположит, что переключение затронет запись"
    )
    assert "запис" in note.lower(), (
        "В пояснении не сказано, что слой записи от профиля не зависит"
    )


@pytest.mark.parametrize("name", ["economy", "standard", "maximum"])
def test_every_profile_sets_every_managed_key(name):
    """Профиль обязан задавать все управляемые ключи целиком.

    Иначе переключение оставляет часть параметров от предыдущего профиля, и
    итоговый набор — не тот, что показан администратору.
    """
    values = profile_settings(name)
    missing = set(PROFILE_KEYS) - set(values)
    assert not missing, f"Профиль {name} не задаёт {sorted(missing)}"
