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


# SPEC §2: камера имеет режим `record_only` (по умолчанию) или `analytics`.
CAMERA_MODES = ("record_only", "analytics")


class DetectionWindow(BaseModel):
    """Окно расписания детекции (SPEC §6).

    `start` больше `end` — окно через полночь (22:00–06:00), а не ошибка:
    §6 называет режим «день/ночь» прямо, и ночная смена — половина
    сценария. Проверка «начало раньше конца» здесь была бы ровно тем
    правилом, которое запрещает половину функции.
    """
    # 0 = понедельник, как datetime.weekday() и как расписание отчётов (§8).
    days: list[int] = Field(default_factory=lambda: list(range(7)))
    start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    @field_validator("days")
    @classmethod
    def _check_days(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("день недели вне диапазона 0..6")
        # Дубликаты не ошибка, но и хранить их незачем — порядок сохраняем,
        # чтобы форма не переставляла отмеченные дни под пользователем.
        seen, out = set(), []
        for d in v:
            if d not in seen:
                seen.add(d)
                out.append(d)
        return out


class DetectionScheduleIn(BaseModel):
    """Расписание детекции камеры (SPEC §6).

    `enabled: false` (и отсутствие расписания вовсе) — «детекция
    круглосуточно». Трактовка «пусто = выключено» на обновлении разом
    остановила бы аналитику на всех существующих камерах без единого
    сообщения; см. worker/detection_schedule.py.
    """
    enabled: bool = False
    # Потолок совпадает с MAX_WINDOWS воркера: расписание правится в
    # браузере и уезжает в JSON-колонку, разбор которой идёт в цикле кадров.
    windows: list[DetectionWindow] = Field(default_factory=list, max_length=10)


class CameraIn(BaseModel):
    name: str
    rtsp_url: str                      # основной поток: запись и просмотр
    sub_rtsp_url: str | None = None    # субпоток: детекция (SPEC §6)
    location: str = ""
    enabled: bool = True
    # Режим по умолчанию — только запись: аналитика включается явно на
    # выбранных камерах (SPEC §1, §24).
    mode: str = Field(default="record_only", pattern="^(record_only|analytics)$")
    motion_sensitivity: int | None = None
    # ТЗ 18.7: события движения/присутствия людей напрямую от ONVIF-камеры
    onvif_enabled: bool = False
    onvif_host: str | None = None
    onvif_port: int | None = None
    onvif_username: str | None = None
    onvif_password: str | None = None  # пусто при PUT — оставить прежний пароль без изменений
    # SPEC §5: глубина хранения по камере. None — следовать за глобальной
    # настройкой (а не «ноль дней»), см. models.Camera.retention_days.
    retention_days: int | None = Field(default=None, ge=1, le=3650)
    # SPEC §6: расписание детекции (день/ночь, рабочие часы). None —
    # круглосуточно.
    detection_schedule: DetectionScheduleIn | None = None
    # SPEC §6: «запись только при движении (опционально)». Дефолт False —
    # и в форме камеры, и при импорте конфигурации: режим удаляет записанное,
    # и включаться он должен только явным действием администратора.
    record_on_motion: bool = False

    _check_rtsp_url = field_validator("rtsp_url")(_validate_rtsp_url_required)
    _check_sub_rtsp_url = field_validator("sub_rtsp_url")(_validate_rtsp_url_optional)


class CameraOut(BaseModel):
    id: int
    name: str
    location: str
    enabled: bool
    mode: str = "record_only"
    status: str
    has_substream: bool = False
    motion_sensitivity: int | None = None
    onvif_enabled: bool = False
    has_onvif: bool = False
    retention_days: int | None = None
    # Адрес, порт и логин ONVIF отдаются, чтобы форма редактирования могла
    # вернуть их обратно без изменений. Без этого «Изм.» открывала пустые
    # поля, и сохранение затирало ONVIF-настройки камеры: PUT трактует
    # пустой `onvif_host` как «убрать». Пароль остаётся write-only —
    # пустое значение при PUT означает «оставить прежний».
    onvif_host: str | None = None
    onvif_port: int | None = None
    onvif_username: str | None = None
    # SPEC §6: расписание отдаётся целиком — форма редактирования должна
    # вернуть его обратно без изменений, иначе сохранение любой другой
    # правки камеры стирало бы расписание (ровно та ошибка, что уже была с
    # ONVIF-полями выше).
    detection_schedule: dict | None = None
    # SPEC §6: та же причина, что и у расписания выше — форма обязана
    # вернуть флаг обратно без изменений, иначе правка любого другого поля
    # камеры молча выключала бы режим.
    record_on_motion: bool = False

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


class OnvifDescribeRequest(OnvifProfilesRequest):
    """Скоупы приходят вместе с устройством из WS-Discovery — они содержат
    имя, заданное в веб-интерфейсе камеры, и служат источником имени, когда
    у камеры нет текстового OSD."""
    scopes: list[str] = []


class OnvifBulkAddItem(OnvifDescribeRequest):
    # Имя можно переопределить в интерфейсе до добавления; пусто — берётся
    # предложенное (OSD → скоуп → модель → IP).
    name: str | None = None
    location: str = ""


class OnvifBulkAddRequest(BaseModel):
    """Массовое добавление найденных камер. Учётные данные у каждой камеры
    свои: в сети может быть смесь устройств с разными паролями, а требовать
    единый пароль на все — лишнее ограничение."""
    cameras: list[OnvifBulkAddItem]
    enabled: bool = True
    onvif_enabled: bool = True


class PtzMoveIn(BaseModel):
    """Скорости поворота и зума в нормализованном пространстве ONVIF.

    Границы проверяются здесь, а не только в воркере: значение вне [-1, 1]
    часть прошивок трактует по модулю и уводит камеру в сторону,
    противоположную нажатой стрелке, а `nan`/`inf` из JSON вообще не имеют
    смысла как скорость. Отказ на границе API дешевле, чем разбирательство,
    почему купол уехал.
    """
    pan: float = Field(0.0, ge=-1.0, le=1.0, allow_inf_nan=False)
    tilt: float = Field(0.0, ge=-1.0, le=1.0, allow_inf_nan=False)
    zoom: float = Field(0.0, ge=-1.0, le=1.0, allow_inf_nan=False)
    # Токен PTZ-профиля, полученный из GET /api/cameras/{id}/ptz. Без него
    # воркер резолвит профиль сам — лишний GetProfiles на каждую команду,
    # поэтому интерфейс его передаёт.
    profile_token: str | None = None


class PtzPresetGotoIn(BaseModel):
    preset_token: str = Field(min_length=1, max_length=128)
    profile_token: str | None = None


class PtzPresetSaveIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    profile_token: str | None = None


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


class TimelineSegmentOut(BaseModel):
    """Звено цепочки воспроизведения шкалы (SPEC §5).

    Без `file_path`, в отличие от `SegmentOut`: плееру он не нужен — файл
    он берёт по `/api/archive/file/{id}` — а шкала суток это до 1440 строк,
    то есть столько же серверных путей в разметке страницы без единого
    применения.
    """
    id: int
    started_at: datetime
    ended_at: datetime
    duration_sec: int

    class Config:
        from_attributes = True


class TimelineRangeOut(BaseModel):
    """Непрерывный кусок записи: между `start` и `end` дыр нет."""
    start: datetime
    end: datetime


class TimelineOut(BaseModel):
    camera_id: int
    date_from: datetime
    date_to: datetime
    ranges: list[TimelineRangeOut]
    segments: list[TimelineSegmentOut]
    # Секунды записи внутри окна — по склеенным диапазонам, не суммой
    # длительностей: перекрытия не должны давать «25 часов из 24».
    recorded_sec: float
    # Выдача упёрлась в потолок сегментов: шкала показывает не всё окно.
    # Флаг существует, чтобы страница сказала это вслух — обрезанная шкала
    # выглядит как «дальше записи нет».
    truncated: bool
