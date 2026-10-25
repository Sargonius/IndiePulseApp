from __future__ import annotations
from datetime import datetime
from typing import Optional
from enum import Enum
from sqlalchemy import UniqueConstraint
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
    tg = "tg"
    bluesky = "bluesky"
    ig_reels = "ig_reels"

class BriefTypeEnum(str, Enum):
    news = "news"
    post = "post"
    video = "video"

class PostStatusEnum(str, Enum):
    queued = "queued"
    running = "running"
    done = "done"
    error = "error"
    canceled = "canceled"
    # Uploaded to the platform's drafts/inbox (e.g. TikTok), waiting for the
    # user to finish & publish manually inside the platform's app.
    awaiting_finish = "awaiting_finish"

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
    """Shorts-Factory "clip type" preset (e.g. レベルデザイン / UI作業 / カメラワーク).

    Applied PER CLIP (unlike Project, which is a whole-batch preset). One click
    fills a card's title + description + tags + music. Orthogonal to Project:
    a Project is "which game", a Preset is "what kind of clip".
    """
    __tablename__ = "presets"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(default=1, index=True)
    name: str = Field(index=True)
    tags_json: str = Field(default="")
    title: Optional[str] = Field(default=None)
    text_en: Optional[str] = Field(default=None)  # description (any language)
    music_path: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)

class Project(SQLModel, table=True):
    __tablename__ = "projects"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(default=1, index=True)
    name: str = Field(index=True)
    icon_path: Optional[str] = Field(default=None)
    tags_json: Optional[str] = Field(default=None)
    platforms_json: Optional[str] = Field(default=None)
    sort_order: int = Field(default=0)
    # Shorts-Factory render preset (applied to every clip when the project is picked)
    crop_x: Optional[int] = Field(default=None)
    crop_y: Optional[int] = Field(default=None)
    crop_scale: Optional[float] = Field(default=None)
    music_path: Optional[str] = Field(default=None)
    music_offset_sec: Optional[int] = Field(default=None)
    fade_in_ms: Optional[int] = Field(default=None)
    fade_out_ms: Optional[int] = Field(default=None)
    platforms_csv: Optional[str] = Field(default=None)  # e.g. "yt,tiktok,ig_reels"
    # Marketing: the home screen groups metrics, goals and posts by project
    steam_app_id: Optional[int] = Field(default=None)
    stage: Optional[str] = Field(default=None)   # key of marketing.PROJECT_STAGES
    status: Optional[str] = Field(default=None)  # active (None) | paused | archived
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class Brief(SQLModel, table=True):
    __tablename__ = "briefs"
    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True, foreign_key="users.id")
    type: BriefTypeEnum
    text_ru: Optional[str] = None
    title: Optional[str] = None
    text_en: Optional[str] = None
    # The text in every language of Settings → Languages ({"ru": ..., "en": ..., "ja": ...}).
    # text_ru / text_en stay as mirrors of its "ru" / "en": read and write texts through
    # app.services.languages (brief_texts / set_brief_texts), which keeps them in step.
    texts_json: Optional[str] = None
    tags_json: Optional[str] = None
    media_path: Optional[str] = None
    media_normal_path: Optional[str] = None  # Horizontal/normal video (16:9)
    media_vertical_path: Optional[str] = None  # Vertical video (9:16)
    music_path: Optional[str] = None
    music_offset_sec: Optional[int] = None
    fade_in_ms: Optional[int] = None
    fade_out_ms: Optional[int] = None
    vertical_916: Optional[bool] = None
    crop_x: Optional[int] = None
    crop_y: Optional[int] = None
    crop_scale: Optional[float] = None
    project_id: Optional[int] = Field(default=None, index=True)
    # Shorts editor: trim (seconds in the source) + text overlays (JSON list,
    # times relative to the trimmed clip; see text_overlay.clean_overlays)
    trim_start: Optional[float] = None
    trim_end: Optional[float] = None
    overlays_json: Optional[str] = None
    # Shorts clip edited in the shared video editor: the whole spec (crop, zoom, trim,
    # sound, music, text; video_edit.clean_spec). When set, render_short renders from it.
    edit_spec_json: Optional[str] = None
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
    # Per-post texts that replace the Brief's at publish time (a short reposted
    # with its own caption/language per network): {title, text_ru, text_en, text_<lang>..., tags_json}
    overrides_json: Optional[str] = None
    # The destination (Settings → Languages & destinations) this post goes to: which channel,
    # page or webhook, and the language of its text. None = a post from before destinations
    # (or a path that doesn't pick one): it publishes to the platform's original target.
    destination_id: Optional[str] = None
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class TikTokItem(SQLModel, table=True):
    """One clip in the TikTok pipeline (/tiktok, logic in app/services/tiktok_pipeline.py).

    Holds what only TikTok needs: the TikTok version of the video (game audio only,
    the sound is added in the app), the caption, the checklist ticks, the viewer
    share typed in from TikTok Studio and the stats history. The TikTok post itself
    is an ordinary Post on the clip's Brief.
    """
    __tablename__ = "tiktok_items"
    id: Optional[int] = Field(default=None, primary_key=True)
    brief_id: int = Field(index=True)
    post_id: Optional[int] = Field(default=None, index=True)
    source: str = Field(default="short")       # short | upload | factory (adopted)
    lang: str = Field(default="ja")
    caption: str = Field(default="")
    tags: str = Field(default="")
    video_rel: Optional[str] = None            # media/ file that goes to TikTok
    render_id: Optional[str] = None            # video_edit job while that file renders
    render_error: Optional[str] = None
    open_id: Optional[str] = None              # the TikTok account it was sent to
    publish_id: Optional[str] = None
    video_id: Optional[str] = None
    tt_status: Optional[str] = None            # last /post/publish/status/fetch/ answer
    tt_error: Optional[str] = None
    status_checked_at: Optional[datetime] = None
    checks_json: Optional[str] = None          # manual ticks {key: true}
    # % of viewers from the target region (Settings → TikTok; its top viewer country for a clip in another
    # language), typed in from TikTok Studio. Named when Japan was the only target; `region_share` in the API
    jp_share: Optional[float] = None
    history_json: Optional[str] = None         # [[utc iso, views, likes, comments, shares], ...]
    archived: bool = Field(default=False)
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


# ── Marketing: metrics and goals (logic in app/services/marketing.py) ────────
class MetricPoint(SQLModel, table=True):
    """One value of a marketing metric for one day.

    project_id 0 = account-wide (Discord server, YouTube channel). dim "" is the
    total; breakdowns use "country:JP" / "lang:japanese". A manual entry that
    covers a whole week is stored once on its last day with span_days=7.
    """
    __tablename__ = "metric_points"
    __table_args__ = (UniqueConstraint("project_id", "metric", "dim", "day", name="uq_metric_point"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(default=0, index=True)
    metric: str = Field(index=True)
    dim: str = Field(default="")
    day: str = Field(index=True)  # YYYY-MM-DD
    value: float = Field(default=0.0)
    span_days: int = Field(default=1)
    source: str = Field(default="")
    updated_at: datetime = Field(default_factory=now_ts)

class AudienceSample(SQLModel, table=True):
    """A follower count read at one moment (ts is UTC, like Post.published_at).

    metric_points keeps one value per day for charts and goals; these timestamped
    reads answer "how many followers were there right before this publication".
    project_id 0 = a social account, otherwise the game (Steam page followers).
    """
    __tablename__ = "audience_samples"
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(default=0, index=True)
    metric: str = Field(index=True)
    ts: datetime = Field(index=True)
    value: float = Field(default=0.0)

class Goal(SQLModel, table=True):
    __tablename__ = "goals"
    id: Optional[int] = Field(default=None, primary_key=True)
    title: str
    project_id: int = Field(default=0, index=True)  # 0 = global goal
    metric: str
    dim: str = Field(default="")
    mode: str = Field(default="delta")  # delta: +target from start | total: reach target
    target: float
    start_day: str
    deadline: Optional[str] = Field(default=None)
    baseline: Optional[float] = Field(default=None)  # level metrics: value at start
    status: str = Field(default="active")  # active | achieved | archived
    achieved_day: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class GoalLink(SQLModel, table=True):
    """A publication (Brief, i.e. every platform post of it) made for a goal."""
    __tablename__ = "goal_links"
    __table_args__ = (UniqueConstraint("goal_id", "brief_id", name="uq_goal_link"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    goal_id: int = Field(index=True)
    brief_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=now_ts)

class Activity(SQLModel, table=True):
    """A manual marketing action to mark on charts ("wrote 15 streamers")."""
    __tablename__ = "activities"
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(default=0, index=True)
    goal_id: Optional[int] = Field(default=None, index=True)
    day: str
    title: str
    created_at: datetime = Field(default_factory=now_ts)

class KVState(SQLModel, table=True):
    """Small key/value store for collector bookmarks and last-run status."""
    __tablename__ = "kv_state"
    key: str = Field(primary_key=True)
    value: str = Field(default="")
    updated_at: datetime = Field(default_factory=now_ts)

class UtmLink(SQLModel, table=True):
    """A saved Steam (or custom) link with UTM tags (/utm, logic in app/services/utm.py).

    base_url None = the project's Steam store page. content "" (not NULL) so the
    unique key also catches duplicates without a content label.
    """
    __tablename__ = "utm_links"
    __table_args__ = (UniqueConstraint("project_id", "source", "medium", "campaign", "content",
                                       name="uq_utm_link"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    source: str
    medium: str
    campaign: str
    content: str = Field(default="")
    base_url: Optional[str] = Field(default=None)
    note: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)


# ── Creator outreach (/outreach, logic in app/services/outreach*.py) ─────────
class OutreachGame(SQLModel, table=True):
    """A game similar to one of ours: creators who covered it are outreach candidates.

    status: suggested (from Steam, waits for a yes) | active (searched) | rejected.
    """
    __tablename__ = "outreach_games"
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    steam_app_id: Optional[int] = Field(default=None, index=True)
    name: str
    source: str = Field(default="manual")  # manual | steam_similar
    status: str = Field(default="suggested")
    similarity: Optional[float] = Field(default=None)  # 0..1 tag overlap with the project
    reviews: Optional[int] = Field(default=None)  # Steam review count: how big the game is
    positive: Optional[int] = Field(default=None)  # % positive reviews
    tags_json: Optional[str] = Field(default=None)  # top Steam tags
    search_query: Optional[str] = Field(default=None)  # YouTube query override (default: the name)
    searched_at: Optional[datetime] = Field(default=None)
    videos_found: int = Field(default=0)
    created_at: datetime = Field(default_factory=now_ts)

class Creator(SQLModel, table=True):
    """A YouTuber / streamer, shared by all projects.

    YouTube API data must be refreshed or deleted within 30 days (stats_at).
    emails_json: [{"email", "source", "found_at"}]; links_json: [url, ...].
    """
    __tablename__ = "creators"
    __table_args__ = (UniqueConstraint("platform", "channel_id", name="uq_creator"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    platform: str = Field(index=True)  # youtube | twitch | other
    channel_id: str  # YouTube UC… id, Twitch login, or the page URL
    name: str
    handle: Optional[str] = Field(default=None)
    url: Optional[str] = Field(default=None)
    avatar_url: Optional[str] = Field(default=None)
    subscribers: Optional[int] = Field(default=None)
    views: Optional[int] = Field(default=None)
    videos: Optional[int] = Field(default=None)
    country: Optional[str] = Field(default=None)
    language: Optional[str] = Field(default=None)  # guessed: en, ru, ja, …
    description: Optional[str] = Field(default=None)
    emails_json: Optional[str] = Field(default=None)
    links_json: Optional[str] = Field(default=None)
    last_upload_at: Optional[datetime] = Field(default=None)
    stats_at: Optional[datetime] = Field(default=None)
    links_scanned_at: Optional[datetime] = Field(default=None)
    ignored: bool = Field(default=False)  # brand/publisher channel: hidden everywhere
    do_not_contact: bool = Field(default=False)  # asked not to be emailed
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class CreatorVideo(SQLModel, table=True):
    """Why a creator is on the list: their video about one of the similar games."""
    __tablename__ = "creator_videos"
    __table_args__ = (UniqueConstraint("video_id", "game_id", name="uq_creator_video"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    creator_id: int = Field(index=True)
    game_id: int = Field(index=True)  # OutreachGame.id
    video_id: str
    title: str
    published_at: Optional[datetime] = Field(default=None)
    views: Optional[int] = Field(default=None)
    language: Optional[str] = Field(default=None)  # defaultAudioLanguage

class OutreachContact(SQLModel, table=True):
    """One creator in one project's pipeline.

    status: found | shortlist | drafted | sent | replied | covered | declined | skipped.
    """
    __tablename__ = "outreach_contacts"
    __table_args__ = (UniqueConstraint("project_id", "creator_id", name="uq_outreach_contact"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    creator_id: int = Field(index=True)
    status: str = Field(default="found", index=True)
    score: float = Field(default=0.0)
    manual: bool = Field(default=False)  # added by hand / CSV, not found by search
    email: Optional[str] = Field(default=None)  # the address the letter goes to
    subject: Optional[str] = Field(default=None)
    body: Optional[str] = Field(default=None)
    key_id: Optional[int] = Field(default=None)
    sent_at: Optional[datetime] = Field(default=None)
    followup_at: Optional[datetime] = Field(default=None)  # when a follow-up is due
    followups: int = Field(default=0)
    replied_at: Optional[datetime] = Field(default=None)
    coverage_url: Optional[str] = Field(default=None)
    notes: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)
    updated_at: datetime = Field(default_factory=now_ts)

class SteamKey(SQLModel, table=True):
    """A Steam key from a Steamworks batch: free → assigned (in a draft) → sent → activated."""
    __tablename__ = "steam_keys"
    __table_args__ = (UniqueConstraint("key", name="uq_steam_key"),)
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(index=True)
    key: str
    batch: str = Field(default="")
    status: str = Field(default="free")  # free | assigned | sent | activated | void
    contact_id: Optional[int] = Field(default=None, index=True)
    assigned_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=now_ts)


# ── ORM hooks ────────────────────────────────────────────────────────────────
# updated_at was never maintained (it always equalled created_at), and error
# texts often embed API URLs/bodies containing tokens. Handle both centrally.
from sqlalchemy import event as _event


def _scrub_errors(target, field: str) -> None:
    val = getattr(target, field, None)
    if val:
        from app.core.redact import redact
        setattr(target, field, redact(val))


@_event.listens_for(Post, "before_insert")
@_event.listens_for(Post, "before_update")
def _post_before_write(mapper, connection, target) -> None:
    target.updated_at = now_ts()
    _scrub_errors(target, "error_text")


@_event.listens_for(Brief, "before_insert")
@_event.listens_for(Brief, "before_update")
def _brief_before_write(mapper, connection, target) -> None:
    # code that still writes text_ru / text_en directly: texts_json follows
    from app.services.languages import sync_mirrors
    sync_mirrors(target)


@_event.listens_for(Job, "before_insert")
@_event.listens_for(Job, "before_update")
def _job_before_write(mapper, connection, target) -> None:
    target.updated_at = now_ts()
    _scrub_errors(target, "last_error")


@_event.listens_for(Creator, "before_update")
@_event.listens_for(OutreachContact, "before_update")
def _touch_updated_at(mapper, connection, target) -> None:
    target.updated_at = now_ts()
