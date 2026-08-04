from datetime import datetime
from sqlalchemy import String, Integer, DateTime, ForeignKey, Boolean, JSON, Text, func, text
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
    event_type: Mapped[str] = mapped_column(String(20))  # motion|face
    duration_sec: Mapped[int] = mapped_column(Integer, default=0)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))


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
