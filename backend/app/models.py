from datetime import datetime
from sqlalchemy import (String, Integer, BigInteger, DateTime, ForeignKey, Boolean, JSON,
                        Text, func, text)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from pgvector.sqlalchemy import Vector
from .db import Base


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))  # admin | operator | viewer
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # ТЗ 13: "парольная политика (срок действия)". Обновляется при создании
    # пользователя и при каждой смене пароля — используется для расчёта
    # истечения (settings.PASSWORD_MAX_AGE_DAYS).
    password_changed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class RefreshToken(Base):
    """ТЗ 13: JWT с refresh-механизмом. Хранится только хэш токена (не сам
    секрет) — как пароль, чтобы утечка БД не давала готовые refresh-токены."""
    __tablename__ = "refresh_tokens"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Отличает "потрачен нормальной ротацией" от "отозван logout'ом/сменой
    # пароля/массовым revoke": повторное предъявление ПЕРВОГО — сигнал кражи
    # (кто-то ещё владеет уже провёрнутым токеном), второго — ожидаемо и не
    # должно обрушивать остальные сессии пользователя.
    rotated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class Camera(Base):
    __tablename__ = "cameras"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    rtsp_url_enc: Mapped[str] = mapped_column(Text)              # основной поток: запись + HLS
    sub_rtsp_url_enc: Mapped[str | None] = mapped_column(Text, nullable=True)  # субпоток: аналитика
    location: Mapped[str] = mapped_column(String(120), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    # SPEC §2, §3: режим камеры. `record_only` — камера только пишется слоем
    # записи (MediaMTX, remux); `analytics` — дополнительно обрабатывается
    # слоем аналитики (детекция и распознавание лиц). По умолчанию
    # `record_only`: SPEC §1 отводит аналитике N выбранных камер (по
    # умолчанию 2) из 120, а §24 прямо выносит «Детекция лиц на всех 120
    # камерах без GPU» за рамки версии.
    mode: Mapped[str] = mapped_column(String(20), default="record_only",
                                      server_default=text("'record_only'"))
    status: Mapped[str] = mapped_column(String(20), default="offline")  # online|offline|disabled
    roi: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # {"polygons": [[[x,y],...]]}
    motion_sensitivity: Mapped[int | None] = mapped_column(Integer, nullable=True)  # переопределяет профиль
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    # ТЗ 18.7: события движения/присутствия людей напрямую от ONVIF-камеры
    # вместо постоянного MOG2-префильтра на CPU.
    onvif_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    onvif_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    onvif_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    onvif_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    onvif_password_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    # SPEC §5, §21: «настраиваемая глубина хранения (глобально и по камерам)».
    # NULL — не «ноль дней», а «следовать за глобальной настройкой»: камера
    # без собственного срока продолжает следовать за ней и после её
    # изменения, чего копия значения в момент создания камеры не дала бы.
    retention_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # SPEC §6: «расписание детекции (день/ночь, рабочие часы)».
    # {"enabled": bool, "windows": [{"days": [0..6], "start": "HH:MM", "end": "HH:MM"}]}
    # NULL и enabled=false — «детекция круглосуточно», а не «никогда»:
    # расписания нет ни у одной существующей камеры, и обратная трактовка
    # на обновлении остановила бы аналитику на объекте молча.
    # Окно с start > end — через полночь; см. worker/detection_schedule.py.
    detection_schedule: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Person(Base):
    __tablename__ = "persons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="unknown")  # known|unknown
    avatar_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    centroid: Mapped[list[float] | None] = mapped_column(Vector(512), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    alert_on_detection: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class FaceEvent(Base):
    __tablename__ = "face_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("persons.id", ondelete="SET NULL"), nullable=True, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    snapshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)       # текущий лучший (улучшенный, если готов)
    orig_snapshot_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # исходный скриншот
    enhanced: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))  # апскейл выполнен
    embedding: Mapped[list[float] | None] = mapped_column(Vector(512), nullable=True)
    bbox: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_known: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))


class VideoSegment(Base):
    __tablename__ = "video_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    camera_id: Mapped[int] = mapped_column(ForeignKey("cameras.id", ondelete="CASCADE"), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ended_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    file_path: Mapped[str] = mapped_column(String(500))
    event_type: Mapped[str] = mapped_column(String(20))  # continuous|motion|face
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)
    # SPEC §21: размер нужен для двух вещей, которые иначе пришлось бы
    # считать обходом файловой системы на каждый запрос, — фактического
    # расхода за сутки (калибровка прогноза против номинала `Mbps × 10.8`)
    # и выбора старейших сегментов под циклическую перезапись.
    # BigInteger: сутки записи одной камеры — ~21.6 ГБ, но колонка держит
    # размер одного сегмента, и 4 байта хватило бы; тип взят с запасом,
    # потому что суммирование по колонке идёт в БД и переполнение SUM на
    # 120 камерах × 14 дней (~36 ТБ) на int4 было бы реальным.
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0,
                                            server_default=text("0"))


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


class ReportSchedule(Base):
    """Шаблон отчёта и (необязательно) расписание его отправки — SPEC §8.

    Одна сущность на «настраиваемые шаблоны отчётов» и «автоматическую
    отправку по расписанию», а не две: шаблон без расписания — это просто
    сохранённый набор параметров, который оператор запускает кнопкой, а
    расписание без шаблона бессмысленно. Разделение на две таблицы дало бы
    осиротевшие расписания при удалении шаблона и ничего не упростило бы.
    """

    __tablename__ = "report_schedules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(32))            # services/reports.py: KINDS
    fmt: Mapped[str] = mapped_column(String(8), default="xlsx")
    days: Mapped[int] = mapped_column(Integer, default=7)    # период выборки
    recipients: Mapped[str] = mapped_column(String(500), default="")

    # Расписание. `period`: daily | weekly | monthly. Часы/минуты — по
    # локальному времени сервера: администратор объекта думает в нём же,
    # а не в UTC, и «отчёт в 8 утра» должен приходить к открытию, а не со
    # сдвигом на часовой пояс.
    enabled: Mapped[bool] = mapped_column(Boolean, default=False,
                                          server_default=text("false"))
    period: Mapped[str] = mapped_column(String(16), default="daily")
    hour: Mapped[int] = mapped_column(Integer, default=8)
    minute: Mapped[int] = mapped_column(Integer, default=0)
    # Для weekly — день недели 0..6 (0 = понедельник), для monthly — число
    # 1..28. Верхняя граница 28, а не 31: расписание «31-го числа» молча
    # не сработало бы в феврале, и заметили бы это через месяцы.
    day_of_week: Mapped[int] = mapped_column(Integer, default=0)
    day_of_month: Mapped[int] = mapped_column(Integer, default=1)

    # Момент последней УСПЕШНОЙ отправки. Он же — защита от дублей: слот
    # считается закрытым, если last_sent_at не раньше его начала.
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), index=True)
    username: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(20))
    action: Mapped[str] = mapped_column(String(120))         # human-readable, RU
    method: Mapped[str] = mapped_column(String(10))
    path: Mapped[str] = mapped_column(String(255))
    status_code: Mapped[int] = mapped_column(Integer)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
