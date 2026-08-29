"""Доменная логика ONVIF Profile G (SPEC §12): какие записи есть у системы
и в каких границах во времени, поверх таблицы `video_segments`.

Модель отображения ONVIF ↔ FaceWatch:

* **Recording** = одна камера. Токен записи — `cam{camera_id}` (тот же
  префикс, что у пути MediaMTX и у имени файла сегмента, SPEC §20), обратимо
  разбирается в id. Запись «существует» для ONVIF, только если у камеры есть
  хотя бы один сегмент: пустые камеры в выдаче Profile G — шум для VMS.
* **Track** = один видеотрек на запись, токен `VIDEO_{camera_id}`. FaceWatch
  пишет remux одного видеопотока (SPEC §2), аудио- и метадорожек нет.
* **Границы записи** (`EarliestRecording`/`LatestRecording`, `DataFrom`/
  `DataTo`) — агрегаты `min(started_at)`/`max(ended_at)` сегментов камеры.

Сюда не входит сборка XML (она в маршрутах) — только запросы и структуры
данных, чтобы это можно было тестировать и мерить в отрыве от SOAP.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Camera, VideoSegment


_TOKEN_RE = re.compile(r"^cam(\d+)$")


def recording_token(camera_id: int) -> str:
    return f"cam{camera_id}"


def track_token(camera_id: int) -> str:
    return f"VIDEO_{camera_id}"


def camera_id_from_token(token: str) -> int | None:
    """`cam12` → 12; `VIDEO_12` → 12; иначе None. Принимает и токен трека,
    потому что часть операций Replay/Search приходит с любым из двух."""
    if not token:
        return None
    m = _TOKEN_RE.match(token.strip())
    if m:
        return int(m.group(1))
    if token.startswith("VIDEO_") and token[6:].isdigit():
        return int(token[6:])
    return None


@dataclass
class RecordingInfo:
    camera_id: int
    name: str
    location: str
    earliest: datetime
    latest: datetime

    @property
    def token(self) -> str:
        return recording_token(self.camera_id)

    @property
    def track(self) -> str:
        return track_token(self.camera_id)


@dataclass
class Summary:
    data_from: datetime | None
    data_until: datetime | None
    number_recordings: int


async def get_summary(db: AsyncSession) -> Summary:
    """GetRecordingSummary: общие границы архива и число записей (камер с
    сегментами). Считается одним запросом-агрегатом, а не выборкой всех
    сегментов, — на объекте это сотни тысяч строк (SPEC §7)."""
    row = (await db.execute(
        select(
            func.min(VideoSegment.started_at),
            func.max(VideoSegment.ended_at),
            func.count(func.distinct(VideoSegment.camera_id)),
        )
    )).one()
    return Summary(data_from=row[0], data_until=row[1], number_recordings=row[2] or 0)


async def list_recordings(db: AsyncSession,
                          camera_ids: set[int] | None = None) -> list[RecordingInfo]:
    """Записи (камеры), у которых есть хотя бы один сегмент, с границами.

    `camera_ids` — фильтр области поиска ONVIF (IncludedRecordings/
    IncludedSources). None — все записи. Пустое множество означает «фильтр
    задан, но ничему не соответствует» и честно даёт пустой список, а не
    «все»: иначе VMS, спросивший конкретную камеру, получил бы чужие записи.
    """
    q = (
        select(
            VideoSegment.camera_id,
            func.min(VideoSegment.started_at),
            func.max(VideoSegment.ended_at),
            Camera.name,
            Camera.location,
        )
        .join(Camera, Camera.id == VideoSegment.camera_id)
        .group_by(VideoSegment.camera_id, Camera.name, Camera.location)
        .order_by(VideoSegment.camera_id)
    )
    if camera_ids is not None:
        if not camera_ids:
            return []
        q = q.where(VideoSegment.camera_id.in_(camera_ids))

    out: list[RecordingInfo] = []
    for cam_id, earliest, latest, name, location in (await db.execute(q)).all():
        out.append(RecordingInfo(
            camera_id=cam_id, name=name or f"cam{cam_id}",
            location=location or "", earliest=earliest, latest=latest,
        ))
    return out


@dataclass
class SearchScope:
    """Разобранная область FindRecordings.

    `included` — id камер из IncludedRecordings/IncludedSources (None — не
    ограничивать). Токены, которые не разбираются в id камеры, игнорируются
    молча: неизвестный источник — это «нет таких записей», а не ошибка.
    """
    included: set[int] | None = None

    def as_dict(self) -> dict:
        return {"included": sorted(self.included) if self.included is not None else None}

    @staticmethod
    def from_dict(d: dict) -> "SearchScope":
        inc = d.get("included")
        return SearchScope(included=set(inc) if inc is not None else None)


def parse_scope(tokens_recordings: list[str], tokens_sources: list[str]) -> SearchScope:
    """Собирает область из списков токенов записей и источников. Пустые
    списки → None (не ограничивать); непустые → множество id камер."""
    ids: set[int] = set()
    saw_any = False
    for tok in (*tokens_recordings, *tokens_sources):
        saw_any = True
        cam = camera_id_from_token(tok)
        if cam is not None:
            ids.add(cam)
    return SearchScope(included=ids if saw_any else None)
