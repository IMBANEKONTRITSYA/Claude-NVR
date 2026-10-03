"""ТЗ 13: "парольная политика (сложность, срок действия)". Юнит-тесты
сложности пароля (валидация Pydantic-схем) и истечения срока действия
(чистая функция, без обращения к БД)."""
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models import User
from app.routers.auth import _is_password_expired
from app.schemas import PasswordChange, UserCreate


@pytest.mark.parametrize(
    "password",
    [
        "short1A!",       # короче PASSWORD_MIN_LENGTH (10)
        "alllowercase",   # только один класс символов
        "ALLUPPERCASE",   # только один класс символов
        "1234567890",     # только один класс символов
    ],
)
def test_weak_passwords_rejected(password):
    with pytest.raises(ValidationError):
        UserCreate(username="u", password=password, role="operator")


@pytest.mark.parametrize(
    "password",
    [
        "Str0ngPass!",         # буквы разного регистра + цифра + спецсимвол
        "ДлинныйПароль123",    # кириллица тоже считается буквами
    ],
)
def test_strong_passwords_accepted(password):
    u = UserCreate(username="u", password=password, role="operator")
    assert u.password == password


def test_password_change_enforces_same_policy():
    with pytest.raises(ValidationError):
        PasswordChange(old_password="whatever", new_password="weak")

    pc = PasswordChange(old_password="whatever", new_password="Str0ngPass!")
    assert pc.new_password == "Str0ngPass!"


def test_password_not_expired_when_recently_changed():
    user = User(
        username="u", password_hash="x", role="operator",
        password_changed_at=datetime.now(timezone.utc),
    )
    assert _is_password_expired(user) is False


def test_password_expired_after_max_age():
    settings = Settings()
    stale = datetime.now(timezone.utc) - timedelta(days=settings.PASSWORD_MAX_AGE_DAYS + 1)
    user = User(username="u", password_hash="x", role="operator", password_changed_at=stale)
    assert _is_password_expired(user) is True


def test_password_expiry_handles_naive_datetime():
    """SQLAlchemy DateTime без tz (как в реальной БД) возвращает naive
    datetime — функция не должна падать при сравнении с aware now()."""
    settings = Settings()
    stale_naive = datetime.utcnow() - timedelta(days=settings.PASSWORD_MAX_AGE_DAYS + 1)
    user = User(username="u", password_hash="x", role="operator", password_changed_at=stale_naive)
    assert _is_password_expired(user) is True
