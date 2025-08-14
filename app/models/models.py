from __future__ import annotations
from datetime import datetime
from typing import Optional
from enum import Enum
from sqlmodel import SQLModel, Field

def now_ts() -> datetime:
    return datetime.utcnow()

class PlatformEnum(str, Enum):
    vk = "vk"
    x = "x"
    yt = "yt"
    discord = "discord"
    tiktok = "tiktok"
    vk_video = "vk_video"
    fb_ru = "fb_ru"
    fb_en = "fb_en"
    reddit = "reddit"

class BriefTypeEnum(str, Enum):
    news = "news"
    video = "video"

class PostStatusEnum(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"
    canceled = "canceled"

class JobKindEnum(str, Enum):
    render = "render"
    publish = "publish"
    metrics = "metrics"

class JobStatusEnum(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"

class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True)
    created_at: datetime = Field(default_factory=now_ts)

class Connection(SQLModel, table=True):
    __tablename__ = "connections"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")
    platform: PlatformEnum
    auth_json: str
    status: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class Preset(SQLModel, table=True):
    __tablename__ = "presets"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")
    name: str = Field(index=True)
    tags_json: str
    created_at: datetime = Field(default_factory=now_ts)

class Brief(SQLModel, table=True):
    __tablename__ = "briefs"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")
    type: BriefTypeEnum
    text_ru: Optional[str] = None
    title: Optional[str] = None
    text_en: Optional[str] = None
    tags_json: Optional[str] = None
    media_path: Optional[str] = None
    music_path: Optional[str] = None
    music_offset_sec: Optional[int] = None
    fade_in_ms: Optional[int] = None
    fade_out_ms: Optional[int] = None
    vertical_916: Optional[bool] = None
    crop_x: Optional[int] = None
    crop_y: Optional[int] = None
    crop_scale: Optional[float] = None
    created_at: datetime = Field(default_factory=now_ts)

class Post(SQLModel, table=True):
    __tablename__ = "posts"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")
    brief_id: int = Field(index=True, foreign_key="briefs.id")
    platform: PlatformEnum = Field(index=True)
    status: PostStatusEnum = Field(default=PostStatusEnum.queued)
    scheduled_at: Optional[datetime] = None
    published_at: Optional[datetime] = None
    platform_post_id: Optional[str] = None
    platform_url: Optional[str] = None
    metrics_json: Optional[str] = None
    error_text: Optional[str] = None
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class Job(SQLModel, table=True):
    __tablename__ = "jobs"
    id: Optional[int] = Field(default=None, primary_key=True)
    kind: JobKindEnum
    payload_json: str
    status: JobStatusEnum = Field(default=JobStatusEnum.queued)
    attempt: int = 0
    last_error: Optional[str] = None
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)
