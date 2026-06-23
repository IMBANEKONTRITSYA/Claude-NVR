from datetime import datetime
from pydantic import BaseModel, Field


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    username: str


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


class CameraIn(BaseModel):
    name: str
    rtsp_url: str
    location: str = ""
    enabled: bool = True


class CameraOut(BaseModel):
    id: int
    name: str
    location: str
    enabled: bool
    status: str

    class Config:
        from_attributes = True


class PersonOut(BaseModel):
    id: int
    name: str
    status: str
    avatar_path: str | None
    created_at: datetime

    class Config:
        from_attributes = True


class PersonUpdate(BaseModel):
    name: str | None = None
    status: str | None = None


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
