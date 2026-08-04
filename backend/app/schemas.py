import re
from datetime import datetime
from urllib.parse import urlsplit
from pydantic import BaseModel, Field, field_validator
from .config import settings

# ffmpeg/ffprobe/OpenCV принимают URL множества протоколов (file:, http:,
# concat:, subprocess:, srt: и т.д.) — без ограничения схемы эти поля стали
# бы SSRF/LFI-вектором (например, тест-подключение через /api/cameras/test
# запускает ffprobe прямо с введённым URL). RTSP-URL камер должен быть
# только rtsp(s).
_ALLOWED_RTSP_SCHEMES = ("rtsp", "rtsps")


def _check_rtsp_scheme(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in _ALLOWED_RTSP_SCHEMES or not parsed.hostname:
        raise ValueError(
            "RTSP-URL должен начинаться с rtsp:// или rtsps:// и содержать хост"
        )
    return value


def _validate_rtsp_url_required(value: str) -> str:
    return _check_rtsp_scheme(value)


# ТЗ 13: "парольная политика (сложность)". Требуем минимальную длину и не
# менее 3 из 4 классов символов — блокирует и совсем короткие пароли, и
# длинные, но словарные (только строчные буквы).
_PASSWORD_CLASSES = (
    re.compile(r"[a-zа-яё]"),
    re.compile(r"[A-ZА-ЯЁ]"),
    re.compile(r"\d"),
    re.compile(r"[^\w\s]"),
)


def _validate_password_complexity(value: str) -> str:
    if len(value) < settings.PASSWORD_MIN_LENGTH:
        raise ValueError(f"Пароль должен быть не короче {settings.PASSWORD_MIN_LENGTH} символов")
    classes_present = sum(1 for pattern in _PASSWORD_CLASSES if pattern.search(value))
    if classes_present < 3:
        raise ValueError(
            "Пароль должен содержать минимум 3 из 4: строчные буквы, "
            "заглавные буквы, цифры, спецсимволы"
        )
    return value


def _validate_rtsp_url_optional(value: str | None) -> str | None:
    # Пустая строка у sub_rtsp_url — сигнал "очистить субпоток" (cameras.py),
    # это допустимое значение, а не невалидный URL.
    if value is None or value == "":
        return value
    return _check_rtsp_scheme(value)


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: str
    username: str
    # ТЗ 13: "срок действия пароля" — фронтенд принудительно ведёт на смену
    # пароля, не блокируя сам вход (иначе просроченный пароль = lockout без
    # способа его сменить).
    password_expired: bool = False


class RefreshRequest(BaseModel):
    refresh_token: str


class LogoutRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    username: str
    role: str

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    username: str
    password: str
    role: str = Field(pattern="^(admin|operator|viewer)$")

    _check_password = field_validator("password")(_validate_password_complexity)


class PasswordChange(BaseModel):
    old_password: str
    new_password: str

    _check_new_password = field_validator("new_password")(_validate_password_complexity)


class RtspTest(BaseModel):
    rtsp_url: str

    _check_rtsp_url = field_validator("rtsp_url")(_validate_rtsp_url_required)


class CameraIn(BaseModel):
    name: str
    rtsp_url: str                      # основной поток: запись и просмотр
    sub_rtsp_url: str | None = None    # субпоток: детекция (ТЗ 18.1)
    location: str = ""
    enabled: bool = True
    motion_sensitivity: int | None = None
    # ТЗ 18.7: события движения/присутствия людей напрямую от ONVIF-камеры
    onvif_enabled: bool = False
    onvif_host: str | None = None
    onvif_port: int | None = None
    onvif_username: str | None = None
    onvif_password: str | None = None  # пусто при PUT — оставить прежний пароль без изменений

    _check_rtsp_url = field_validator("rtsp_url")(_validate_rtsp_url_required)
    _check_sub_rtsp_url = field_validator("sub_rtsp_url")(_validate_rtsp_url_optional)


class CameraOut(BaseModel):
    id: int
    name: str
    location: str
    enabled: bool
    status: str
    has_substream: bool = False
    motion_sensitivity: int | None = None
    onvif_enabled: bool = False
    has_onvif: bool = False

    class Config:
        from_attributes = True


class PersonOut(BaseModel):
    id: int
    name: str
    status: str
    avatar_path: str | None
    notes: str | None = None
    alert_on_detection: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class PersonUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    notes: str | None = None
    alert_on_detection: bool | None = None


class FaceEventOut(BaseModel):
    id: int
    camera_id: int
    person_id: int | None
    ts: datetime
    snapshot_path: str | None
    is_known: bool

    class Config:
        from_attributes = True


class FaceEventRich(BaseModel):
    id: int
    camera_id: int
    person_id: int | None
    name: str
    ts: datetime
    snapshot_path: str | None
    is_known: bool
    bbox: dict | None = None


class OnvifProfilesRequest(BaseModel):
    host: str
    port: int = 80
    username: str | None = None
    password: str | None = None


class OnvifStreamUriRequest(OnvifProfilesRequest):
    profile_token: str


class ROIIn(BaseModel):
    polygons: list[list[list[float]]]


class SegmentOut(BaseModel):
    id: int
    camera_id: int
    started_at: datetime
    ended_at: datetime
    file_path: str
    event_type: str
    duration_sec: int

    class Config:
        from_attributes = True
