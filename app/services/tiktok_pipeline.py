"""TikTok pipeline (/tiktok): push Shorts that did well through the TikTok account.

One screen: pick a clip → it gets a TikTok version (game audio only: the sound is
added in the TikTok app, where native sounds work) and a caption in the test
language → send it to the TikTok inbox → finish it in the app → IndiePulse sees that
it went live and reads its numbers. The checklist next to each clip is the test's
playbook.

What TikTok tells, and with which scope:
- "posted yet?" — /post/publish/status/fetch/ (video.upload, always granted); the
  video id comes with it once the post is public and through moderation;
- views, likes, comments, shares — /video/query/ (video.list);
- followers — /user/info/ (user.info.stats); the @handle needs user.info.profile;
- viewer countries — nowhere in the API, only in TikTok Studio, so they're typed in.

The TikTok post is an ordinary Post on the clip's Brief (the Shorts board,
publications and goals see it); a TikTokItem keeps the rest. Stages (`stage`):
rendering → ready → sending → inbox → live, plus render_failed and send_failed.
"""
from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional

from loguru import logger
from sqlmodel import Session, select

from app.models.models import (Brief, BriefTypeEnum, Job, JobKindEnum, JobStatusEnum, MetricPoint, PlatformEnum,
                               Post, PostStatusEnum, Project, TikTokItem)
from app.services import repost, video_edit

LANGS = ("ja", "en", "ru")
LANG_NAMES = {"ja": "Japanese", "en": "English", "ru": "Russian"}
# hashtags a new upload starts with (a Short brings its own, translated)
TAG_HINTS = {"ja": "#インディーゲーム #個人開発 #ゲーム開発 #Steam",
             "en": "#indiegame #gamedev #indiedev #Steam",
             "ru": "#инди #геймдев #индиигры #Steam"}
BASE_SCOPES = ("user.info.basic", "video.upload")
STATS_SCOPES = ("video.list", "user.info.stats", "user.info.profile")
CAPTION_LIMIT = 4000        # the TikTok app's caption limit (unofficial, corroborated)
LENGTH_RANGE = (12, 25)     # seconds, the playbook's range (guide.html)
JP_EVENING = (19, 23)       # hours of the day in Japan when TikTok is busiest there
JST = timezone(timedelta(hours=9))
TARGET_SHARE = 30.0         # % of viewers from Japan that says the language beats the geo
SHARE_SAMPLE = 5            # clips with a typed-in share before the test says anything
ACCOUNT_KEY = "tiktok:account"
MAX_HISTORY = 400
_YT_ONLY = re.compile(r"^#(shorts|short|youtubeshorts|ytshorts)$", re.I)
_LINK = re.compile(r"\{steam\}|https?://\S+")
_VIDEO_URL = re.compile(r"tiktok\.com/(?:(?:@[\w.\-]+/)?(?:video|photo)/|v/)(\d{8,25})")
_SHORT_URL = re.compile(r"^https?://(?:vm|vt|www)\.tiktok\.com/(?:t/)?[\w-]+/?$")


class PipelineError(ValueError):
    MESSAGES = {
        "not_connected": "Connect the TikTok account first (top of the page).",
        "already_sent": "This clip is already on its way to TikTok, or live.",
        "not_ready": "The TikTok version of the video isn’t ready yet.",
        "locked": "The video already went to TikTok, so it can’t change now.",
        "no_settings": "This file has no editor settings to change. Use ✂ Edit instead.",
        "no_text": "There’s no text in the frame.",
        "bad_link": "That doesn’t look like a link to a TikTok video.",
        "linked_elsewhere": "This TikTok video is already linked to another clip.",
        "not_a_video": "The file has no picture: it isn’t a video.",
        "no_source": "The clip’s source file is gone, so it can’t be rendered again.",
    }

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or self.MESSAGES.get(code, code))
        self.code = code


# ── connection and account ───────────────────────────────────────────────────

def scopes(cfg: dict) -> set[str]:
    raw = str(cfg.get("tiktok_scope") or "").replace(" ", ",")
    return {s.strip() for s in raw.split(",") if s.strip()}


def connection(cfg: dict) -> dict:
    granted = scopes(cfg)
    return {
        "configured": bool(str(cfg.get("tiktok_client_key") or "").strip()
                           and str(cfg.get("tiktok_client_secret") or "").strip()),
        "connected": bool(str(cfg.get("tiktok_refresh_token") or cfg.get("tiktok_access_token") or "").strip()),
        "scopes": sorted(granted),
        "views": "video.list" in granted,
        "followers": "user.info.stats" in granted,
        "handle": "user.info.profile" in granted,
        "want_stats": bool(cfg.get("tiktok_want_stats")),
        "open_id": str(cfg.get("tiktok_open_id") or ""),
    }


def auth_scopes(cfg: dict) -> str:
    """Scopes for the consent page. Stats scopes only on request: one the app hasn't
    added in the developer portal makes TikTok's consent page fail entirely."""
    return ",".join(BASE_SCOPES + (STATS_SCOPES if cfg.get("tiktok_want_stats") else ()))


def _client(cfg: dict):
    from app.services.tiktok_client import TikTokClient, get_valid_access_token
    token = get_valid_access_token(cfg)
    return TikTokClient(token) if token else None


def account(session: Session, cfg: dict, refresh: bool = False,
            max_age: timedelta = timedelta(minutes=30), client=None) -> dict:
    """The connected account as last read: name, avatar, @handle and counts where the
    scopes allow. `refresh` re-reads it when the copy is older than `max_age`."""
    from app.services.marketing import kv_get, kv_set
    conn = connection(cfg)
    if not conn["connected"]:
        return {}
    cached = kv_get(session, ACCOUNT_KEY, {}) or {}
    if conn["open_id"] and cached.get("open_id") and cached["open_id"] != conn["open_id"]:
        cached = {}  # another account was connected since
    try:
        checked = datetime.fromisoformat(cached.get("checked_at") or "")
    except ValueError:
        checked = None
    if (conn["followers"] and "follower_count" not in cached) or (conn["handle"] and "username" not in cached):
        checked = None  # reconnected with more scopes: read the new fields now
    if not refresh or (checked and datetime.utcnow() - checked < max_age):
        return cached
    fields = ["open_id", "display_name", "avatar_url"]
    if conn["handle"]:
        fields += ["username", "profile_deep_link"]
    if conn["followers"]:
        fields += ["follower_count", "likes_count", "video_count"]
    try:
        client = client or _client(cfg)
        if client is None:
            return cached
        user = client.user_info(fields)
        cached = {k: user.get(k) for k in fields if user.get(k) is not None}
    except Exception as exc:
        from app.core.redact import redact
        cached = dict(cached, error=redact(str(exc))[:300])
    cached["checked_at"] = datetime.utcnow().isoformat(timespec="seconds")
    kv_set(session, ACCOUNT_KEY, cached)
    return cached


def follower_series(session: Session, days: int = 60) -> dict:
    """Daily follower counts (collected with the other networks every 2 hours)."""
    since = (datetime.utcnow() - timedelta(days=days)).date().isoformat()
    rows = session.exec(select(MetricPoint).where(MetricPoint.project_id == 0, MetricPoint.dim == "",
                                                  MetricPoint.metric == "tiktok_followers",
                                                  MetricPoint.day >= since).order_by(MetricPoint.day)).all()
    return {"days": [r.day for r in rows], "values": [r.value for r in rows]}


# ── texts ────────────────────────────────────────────────────────────────────

def clean_caption(text: str) -> str:
    """TikTok captions can't hold links: drop {steam} and URLs, and the ':' they leave."""
    out = []
    for line in (text or "").splitlines():
        cut = _LINK.sub("", line)
        out.append(re.sub(r"[\s:：\-–—]+$", "", cut) if cut != line else cut.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def clean_tags(tags: str, limit: int = 5) -> str:
    out = [t for t in repost.normalize_tags(tags).split() if not _YT_ONLY.match(t)]
    return " ".join(out[:limit])


def default_texts(brief) -> dict:
    """Caption + hashtags a Short starts with on TikTok: its YouTube title and description."""
    src = repost.source_texts(brief)
    title, desc = src["title"], src["desc"]
    if not title:
        body = desc
    elif not desc or desc.startswith(title):
        body = desc or title
    else:
        body = f"{title}\n\n{desc}"
    return {"caption": clean_caption(body), "tags": clean_tags(src["tags"]), "lang": src["lang"]}


def full_caption(item) -> str:
    return "\n\n".join(p for p in ((item.caption or "").strip(), (item.tags or "").strip()) if p)


def _translate_lines(lines: list[str], target: str) -> list[str]:
    """Frame text lines in `target`, one call when the lines can be told apart again."""
    if not any(lines):
        return lines
    if not any("\n" in t for t in lines):
        got = repost.translate({"desc": "\n".join(lines)}, target)["desc"].split("\n")
        got = [g.strip() for g in got if g.strip()]
        if len(got) == len(lines):
            return got
    return [repost.translate({"desc": t}, target)["desc"].strip() or t if t else t for t in lines]


# ── the TikTok version of the video ──────────────────────────────────────────

def _rel(path: Optional[str]) -> Optional[str]:
    """media/ rel of an existing file, None when it's missing or outside media/."""
    if not path:
        return None
    try:
        return video_edit.media_rel(video_edit.media_abs(path))
    except video_edit.EditError:
        return None


@lru_cache(maxsize=256)
def _probe_cached(path: str, mtime: float, size: int) -> dict:
    return video_edit.probe(path)


def file_info(rel: Optional[str]) -> dict:
    """ffprobe of a media/ file (cached by mtime), {} when it doesn't exist."""
    try:
        path = video_edit.media_abs(rel or "")
    except video_edit.EditError:
        return {}
    st = os.stat(path)
    return _probe_cached(path, st.st_mtime, st.st_size)


def brief_spec(brief, duration: Optional[float] = None) -> dict:
    """The clip's editor settings: the factory's own, or rebuilt from the older columns
    (same geometry: cover-scale × zoom, crop at the % spot)."""
    if brief.edit_spec_json:
        try:
            data = json.loads(brief.edit_spec_json)
        except ValueError:
            data = None
        if isinstance(data, dict):
            return video_edit.short_spec(data, duration)
    try:
        overlays = json.loads(brief.overlays_json or "[]")
    except ValueError:
        overlays = []
    return video_edit.short_spec({
        "crop_x": (brief.crop_x if brief.crop_x is not None else 50) / 100.0,
        "crop_y": (brief.crop_y if brief.crop_y is not None else 50) / 100.0,
        "zoom": brief.crop_scale or 1.0,
        "trim_start": brief.trim_start, "trim_end": brief.trim_end,
        "overlays": overlays if isinstance(overlays, list) else [],
        "music": {"path": brief.music_path} if brief.music_path else None,
    }, duration)


def _render(session: Session, item, src_rel: str, spec: dict) -> None:
    from app.services import music_library
    extra = music_library.preset_music_paths(session)
    job = video_edit.start_render(src_rel, spec, music_ok=lambda p: music_library.is_allowed(p, extra=extra))
    item.video_rel = job["out_rel"]
    item.render_id = job["id"] if job.get("status") in ("queued", "running") else None
    item.render_error = None


def _is_vertical(info: dict) -> bool:
    w, h = info.get("width") or 0, info.get("height") or 0
    return bool(w and h) and abs(w / h - 9 / 16) < 0.01 and max(w, h) <= video_edit.MAX_LONG_SIDE


def prepare_video(session: Session, item, brief, overlays: Optional[list[str]] = None) -> None:
    """Point the item at its TikTok file. A Short without baked music keeps its own
    render; otherwise the source is rendered again with the same settings and no music.
    `overlays` replaces the frame text (translated). Every result gets an editor sidecar,
    so ✂ Edit reopens it with its source and settings."""
    src = _rel(brief.media_path)
    vertical = _rel(brief.media_vertical_path)
    if src is None:
        if vertical is None:
            raise PipelineError("no_source")
        item.video_rel, item.render_id, item.render_error = vertical, None, None
        return
    info = file_info(src)
    if not info.get("width"):
        raise PipelineError("not_a_video")
    spec = brief_spec(brief, info["duration"])
    if overlays is not None:
        spec["overlays"] = [dict(o, text=t[:120]) for o, t in zip(spec["overlays"], overlays)]
    if item.source == "upload" and not spec["overlays"] and _is_vertical(info):
        item.video_rel, item.render_id, item.render_error = src, None, None  # already a 9:16 file
        return
    if not spec["music"] and overlays is None and vertical and vertical != src:
        if not video_edit.load_edit(vertical):
            video_edit.write_sidecar(vertical, src, spec)
        item.video_rel, item.render_id, item.render_error = vertical, None, None
        return
    _render(session, item, src, dict(spec, music=None))


def item_spec(item) -> Optional[dict]:
    """Editor settings of the item's file, None for a file made elsewhere."""
    edit = video_edit.load_edit(item.video_rel) if item.video_rel else None
    return (edit or {}).get("spec") if edit else None


def _locked(item, post) -> bool:
    return stage(item, post) in ("sending", "inbox", "live")


def translate_screen_text(session: Session, item, post=None) -> None:
    """Frame text → the item's language, rendered from the same source and settings."""
    if _locked(item, post):
        raise PipelineError("locked")
    edit = video_edit.load_edit(item.video_rel) if item.video_rel else None
    if not edit:
        raise PipelineError("no_settings")
    spec = edit.get("spec") or {}
    texts = [o.get("text") or "" for o in spec.get("overlays") or []]
    if not any(texts):
        raise PipelineError("no_text")
    lines = _translate_lines(texts, item.lang)
    spec = dict(spec, overlays=[dict(o, text=t[:120]) for o, t in zip(spec["overlays"], lines)])
    _render(session, item, edit["src"], spec)


def drop_music(session: Session, item, post=None) -> None:
    if _locked(item, post):
        raise PipelineError("locked")
    edit = video_edit.load_edit(item.video_rel) if item.video_rel else None
    if not edit:
        raise PipelineError("no_settings")
    _render(session, item, edit["src"], dict(edit.get("spec") or {}, music=None))


def rerender(session: Session, item) -> None:
    """After a failed or lost render: the same settings again, or the clip's own."""
    edit = video_edit.load_edit(item.video_rel) if item.video_rel else None
    if edit and _rel(edit.get("src")):
        _render(session, item, edit["src"], edit.get("spec") or {})
        return
    brief = session.get(Brief, item.brief_id)
    if brief is None:
        raise PipelineError("no_source")
    prepare_video(session, item, brief)


def _check_render(item) -> bool:
    """Web process only (renders are its threads). True when the item changed."""
    if not item.render_id:
        return False
    job = video_edit.get_job(item.render_id)
    if job is None:
        if _rel(item.video_rel):
            item.render_id = None
        else:
            item.render_id, item.render_error = None, "The render stopped (IndiePulse restarted?). Render again."
        return True
    if job["status"] == "done":
        item.render_id = None
        item.video_rel = job.get("out_rel") or item.video_rel
        return True
    if job["status"] in ("error", "cancelled"):
        item.render_id, item.render_error = None, str(job.get("error") or job["status"])[-300:]
        return True
    return False


# ── creating items ───────────────────────────────────────────────────────────

def _translate_caption(item, lang: str, notes: list) -> None:
    if not (item.caption or item.tags):
        return
    try:
        got = repost.translate({"desc": item.caption, "tags": item.tags}, lang)
        item.caption = clean_caption(got["desc"]) or item.caption
        item.tags = clean_tags(got["tags"]) or item.tags
    except Exception as exc:
        notes.append(f"Caption not translated: {exc}")


def _frame_lines_in(brief, lang: str, notes: list) -> Optional[list[str]]:
    """The Short's frame text translated into `lang`; None when it already is (or has none)."""
    if brief is None or not _rel(brief.media_path):
        return None
    texts = [o.get("text") or "" for o in brief_spec(brief)["overlays"]]
    joined = "\n".join(texts)
    if not joined.strip() or repost.detect_lang(joined) == lang:
        return None
    try:
        return _translate_lines(texts, lang)
    except Exception as exc:
        notes.append(f"Frame text not translated: {exc}")
        return None


def active_item_for(session: Session, brief_id: int):
    return session.exec(select(TikTokItem).where(TikTokItem.brief_id == brief_id, TikTokItem.archived == False)  # noqa: E712
                        .order_by(TikTokItem.id.desc())).first()


def create_from_brief(session: Session, brief, lang: str) -> tuple[TikTokItem, list[str]]:
    """A Short into the pipeline: caption and frame text translated, TikTok version
    prepared. A Short already in the pipeline returns its item."""
    existing = active_item_for(session, brief.id)
    if existing is not None:
        return existing, []
    lang = lang if lang in LANGS else "ja"
    texts = default_texts(brief)
    item = TikTokItem(brief_id=brief.id, source="short", lang=lang, caption=texts["caption"], tags=texts["tags"])
    notes: list[str] = []
    if texts["lang"] != lang:
        _translate_caption(item, lang, notes)
    overlays = _frame_lines_in(brief, lang, notes)
    try:
        prepare_video(session, item, brief, overlays=overlays)
    except (PipelineError, video_edit.EditError) as exc:
        item.render_error = str(exc)
    session.add(item)
    session.commit()
    session.refresh(item)
    return item, notes


def create_from_upload(session: Session, rel: str, project_id: Optional[int], lang: str) -> TikTokItem:
    """A dropped video (already uploaded to media/) into the pipeline, on a new Brief."""
    from app.services import shorts_upload
    rel = shorts_upload.uploaded_rel(rel)
    lang = lang if lang in LANGS else "ja"
    stem = os.path.splitext(re.sub(r"^short_[0-9a-f]{12}_", "", rel))[0].replace("_", " ").strip()
    project = session.get(Project, project_id) if project_id else None
    brief = Brief(user_id=1, type=BriefTypeEnum.video, vertical_916=True, media_path=rel,
                  title=stem[:120] or None, project_id=project.id if project else None)
    session.add(brief)
    session.commit()
    session.refresh(brief)
    item = TikTokItem(brief_id=brief.id, source="upload", lang=lang, caption="", tags=TAG_HINTS[lang])
    try:
        prepare_video(session, item, brief)
    except (PipelineError, video_edit.EditError) as exc:
        item.render_error = str(exc)
    session.add(item)
    session.commit()
    session.refresh(item)
    _sync_upload_brief(session, item)
    return item


def set_lang(session: Session, item, lang: str) -> list[str]:
    """Switch the clip to another language: caption, hashtags and frame text."""
    if lang not in LANGS:
        raise PipelineError("bad_lang", f"unknown language: {lang}")
    post = session.get(Post, item.post_id) if item.post_id else None
    notes: list[str] = []
    if item.source == "upload" and item.tags == TAG_HINTS.get(item.lang):
        item.tags = TAG_HINTS[lang]  # the starter hashtags: the other language's set
    item.lang = lang
    _translate_caption(item, lang, notes)
    if not _locked(item, post):
        spec = item_spec(item)
        texts = "\n".join(o.get("text") or "" for o in (spec or {}).get("overlays") or [])
        if texts.strip() and repost.detect_lang(texts) != lang:
            try:
                translate_screen_text(session, item, post)
            except Exception as exc:
                notes.append(f"Frame text not translated: {exc}")
    _touch(session, item)
    return notes


def _sync_upload_brief(session: Session, item) -> None:
    """A dropped video's Brief shows its TikTok file on the Shorts board once it exists."""
    if item.source != "upload" or item.render_id or not _rel(item.video_rel):
        return
    brief = session.get(Brief, item.brief_id)
    if brief is not None and brief.media_vertical_path != item.video_rel:
        brief.media_vertical_path = item.video_rel
        session.add(brief)
        session.commit()


def _touch(session: Session, item) -> None:
    item.updated_at = datetime.utcnow()
    session.add(item)
    session.commit()
    session.refresh(item)


def update(session: Session, item, data: dict) -> None:
    """Edits from the card: caption, tags, ticks, the Japan share, a file from ✂ Edit."""
    post = session.get(Post, item.post_id) if item.post_id else None
    if "caption" in data:
        item.caption = str(data.get("caption") or "")[:CAPTION_LIMIT]
    if "tags" in data:
        item.tags = repost.normalize_tags(str(data.get("tags") or ""))[:500]
    if isinstance(data.get("checks"), dict):
        ticks = _ticks(item)
        for key, on in data["checks"].items():
            if key in CHECK_KEYS:
                ticks[key] = bool(on)
        item.checks_json = json.dumps({k: True for k, v in ticks.items() if v})
    if "jp_share" in data:
        raw = data.get("jp_share")
        if raw in (None, ""):
            item.jp_share = None
        else:
            try:
                item.jp_share = round(max(0.0, min(100.0, float(raw))), 1)
            except (TypeError, ValueError):
                raise PipelineError("bad_share", "The share is a number from 0 to 100.")
    if data.get("video_rel"):
        if _locked(item, post):
            raise PipelineError("locked")
        rel = _rel(str(data["video_rel"]))
        if rel is None:
            raise PipelineError("not_ready", "That file isn’t in media/.")
        item.video_rel, item.render_id, item.render_error = rel, None, None
    _touch(session, item)
    _sync_upload_brief(session, item)


def archive(session: Session, item) -> None:
    item.archived = True
    _touch(session, item)


# ── sending, going live ──────────────────────────────────────────────────────

def stage(item, post) -> str:
    st = getattr(getattr(post, "status", None), "value", None) or (str(post.status) if post is not None else None)
    if st == "done":
        return "live"
    if st in ("queued", "running"):
        return "sending"
    if st == "awaiting_finish":
        return "inbox"
    if st in ("error", "canceled"):
        return "send_failed"
    if item.render_id:
        return "rendering"
    if item.render_error or not _rel(item.video_rel):
        return "render_failed"
    return "ready"


def _new_post(item, cfg: Optional[dict] = None) -> Post:
    """The TikTok post of a clip: to the TikTok destination of the clip's language when there is one (Settings →
    Languages & destinations; the caption as that language's text), else as before — no destination, the caption
    as text_en."""
    from app.services import destinations
    dest = next((d for d in destinations.all_destinations(cfg) if d["module"] == "tiktok" and d["enabled"]
                 and d["lang"] == item.lang), None)
    overrides = {"text_en": "", "text_ru": "", "tags_json": ""}
    overrides[f"text_{item.lang}" if dest else "text_en"] = full_caption(item)
    return Post(user_id=1, brief_id=item.brief_id, platform=PlatformEnum.tiktok, status=PostStatusEnum.queued,
                destination_id=dest["id"] if dest else None, overrides_json=json.dumps(overrides, ensure_ascii=False))


def send(session: Session, item, cfg: dict) -> None:
    """Queue the upload of the TikTok version to the account's TikTok inbox."""
    from app.jobs.tasks import publish_news_post
    from app.services import duplicates
    post = session.get(Post, item.post_id) if item.post_id else None
    if _locked(item, post):
        raise PipelineError("already_sent")
    if stage(item, None) != "ready":
        raise PipelineError("not_ready")
    if not connection(cfg)["connected"]:
        raise PipelineError("not_connected")
    post = _new_post(item, cfg)
    session.add(post)
    session.flush()  # the id, in the same transaction as the item (the tick adopts unclaimed posts)
    # allow_duplicate: the clip may have a draft from the old, lost TikTok account
    payload = {"post_id": post.id, "media_rel": item.video_rel, duplicates.ALLOW: True, "tiktok_item": item.id}
    job = Job(kind=JobKindEnum.publish, payload_json=json.dumps(payload, ensure_ascii=False),
              status=JobStatusEnum.queued)
    session.add(job)
    item.post_id = post.id
    item.open_id = connection(cfg)["open_id"] or None
    item.publish_id = item.video_id = item.tt_status = item.tt_error = None
    item.status_checked_at = None
    item.updated_at = datetime.utcnow()
    session.add(item)
    session.commit()
    session.refresh(job)
    publish_news_post.schedule(args=(job.id, post.id), delay=0)
    logger.info(f"tiktok pipeline item={item.id}: queued post={post.id} file={item.video_rel}")


def id_time(video_id: str) -> Optional[datetime]:
    """A TikTok video id starts with its creation time: id >> 32 = unix seconds (UTC)."""
    try:
        ts = int(video_id) >> 32
    except (TypeError, ValueError):
        return None
    if not 1_450_000_000 < ts < datetime.utcnow().timestamp() + 86400:
        return None
    return datetime.utcfromtimestamp(ts)


def video_url(username: str, video_id: str) -> Optional[str]:
    return f"https://www.tiktok.com/@{username}/video/{video_id}" if username and video_id else None


def _go_live(item, post, video_id: Optional[str], now: datetime, url: Optional[str] = None,
             username: str = "") -> None:
    if post.status != PostStatusEnum.done:
        post.status = PostStatusEnum.done
        post.published_at = now
        post.error_text = None
    if video_id:
        item.video_id = str(video_id)
        post.platform_post_id = str(video_id)
        post.published_at = id_time(video_id) or post.published_at
        post.platform_url = url or post.platform_url or video_url(username, str(video_id))
    post.updated_at = now


def parse_link(url: str) -> tuple[str, str]:
    """(video id, link) from a TikTok video link; short vm./vt. links are followed."""
    url = (url or "").strip()
    if url.isdigit():
        return url, ""
    m = _VIDEO_URL.search(url)
    if not m and _SHORT_URL.match(url):
        import httpx
        try:
            r = httpx.get(url, follow_redirects=True, timeout=10,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            url = str(r.url)
            m = _VIDEO_URL.search(url)
        except Exception as exc:
            logger.warning(f"tiktok short link {url}: {exc}")
    if not m:
        raise PipelineError("bad_link")
    return m.group(1), url.split("?", 1)[0]


def link(session: Session, item, url: str) -> None:
    """The clip is live on TikTok (posted by hand, or TikTok hasn't said so yet): link it."""
    vid, clean = parse_link(url)
    other = session.exec(select(TikTokItem).where(TikTokItem.video_id == vid, TikTokItem.id != item.id,
                                                  TikTokItem.archived == False)).first()  # noqa: E712
    if other is not None:
        raise PipelineError("linked_elsewhere")
    post = session.get(Post, item.post_id) if item.post_id else None
    if stage(item, post) == "sending":  # the upload would mark it a draft again when it ends
        raise PipelineError("busy", "The upload is still running. Link the video once it’s in the inbox.")
    if post is None or stage(item, post) == "send_failed":
        post = _new_post(item)
        session.add(post)
        session.flush()
        item.post_id = post.id
    _go_live(item, post, vid, datetime.utcnow(), url=clean or None)
    session.add(post)
    _touch(session, item)


def _poll_status(client, item, post, now: datetime, username: str) -> None:
    from app.services.tiktok_client import TikTokAPIError
    try:
        data = client.check_status(item.publish_id)
    except TikTokAPIError as exc:
        item.tt_error = ("This draft was sent from another TikTok account." if exc.code == "invalid_publish_id"
                         else str(exc)[:300])
        item.status_checked_at = now
        return
    except Exception as exc:  # network: try again next time
        item.tt_error = f"status check failed: {exc}"[:300]
        item.status_checked_at = now
        return
    item.status_checked_at = now
    item.tt_error = None
    status = str(data.get("status") or "")
    item.tt_status = status or item.tt_status
    ids = [str(x) for x in (data.get("publicaly_available_post_id") or data.get("publicly_available_post_id") or [])
           if str(x).strip()]
    if status == "FAILED":
        post.status = PostStatusEnum.error
        post.error_text = f"tiktok_failed: {data.get('fail_reason') or 'unknown'}"
        post.updated_at = now
    elif status == "PUBLISH_COMPLETE" or ids:
        _go_live(item, post, ids[0] if ids else None, now, username=username)


def _checked_at(post) -> Optional[datetime]:
    try:
        return datetime.fromisoformat((json.loads(post.metrics_json or "{}") or {}).get("checked_at") or "")
    except (TypeError, ValueError, AttributeError):
        return None


def _metrics_due(post, now: datetime, force: bool) -> bool:
    checked = _checked_at(post)
    if force:
        return checked is None or now - checked >= timedelta(minutes=2)
    age = now - (post.published_at or now)
    if age < timedelta(days=2):
        gap = timedelta(minutes=29)
    elif age < timedelta(days=7):
        gap = timedelta(hours=3)
    elif age < timedelta(days=60):
        gap = timedelta(hours=23)
    else:
        return False
    return checked is None or now - checked >= gap


def _push_history(item, post, m: dict, now: datetime) -> None:
    try:
        hist = json.loads(item.history_json or "[]")
    except ValueError:
        hist = []
    point = [now.isoformat(timespec="seconds"), m.get("views"), m.get("likes"), m.get("comments"), m.get("reposts")]
    if not hist and post.published_at:
        hist.append([post.published_at.isoformat(timespec="seconds"), 0, 0, 0, 0])
    if hist and hist[-1][1:] == point[1:]:
        return
    hist.append(point)
    item.history_json = json.dumps(hist[-MAX_HISTORY:])


def adopt_factory_posts(session: Session, cfg: dict) -> int:
    """TikTok drafts the Shorts Factory sent to the connected account become items too,
    so every clip of the account is on this screen."""
    try:
        since = datetime.utcfromtimestamp(int(cfg.get("tiktok_connected_at") or 0))
    except (TypeError, ValueError, OverflowError):
        return 0
    if since.year < 2020:
        return 0
    from app.jobs.tasks import publish_payload_for
    from app.platforms.common import post_overrides
    from app.services import destinations, languages
    taken = set(session.exec(select(TikTokItem.post_id).where(TikTokItem.post_id.is_not(None))).all())
    posts = session.exec(select(Post).where(Post.platform == PlatformEnum.tiktok, Post.created_at >= since,
                                            Post.status.in_([PostStatusEnum.queued, PostStatusEnum.running,
                                                             PostStatusEnum.awaiting_finish, PostStatusEnum.done]))
                         ).all()
    made = 0
    for post in posts:
        if post.id in taken:
            continue
        brief = session.get(Brief, post.brief_id)
        if brief is None:
            continue
        texts = default_texts(brief)
        own = post_overrides(post)
        dest = destinations.get(post.destination_id, cfg) if post.destination_id else None
        if dest is not None:  # the factory's post to a TikTok destination: the text in its language
            mine = own.get(f"text_{dest['lang']}") or languages.brief_text(brief, dest["lang"], cfg)
        else:
            mine = own.get("text_en") or own.get("text_ru") or ""
        caption = clean_caption(mine) or texts["caption"]
        media = _rel(publish_payload_for(session, post.id).get("media_rel")) or _rel(brief.media_vertical_path)
        item = TikTokItem(brief_id=brief.id, post_id=post.id, source="factory",
                          lang=repost.detect_lang(caption) or texts["lang"], caption=caption,
                          tags=clean_tags(own.get("tags_json") or "") or texts["tags"], video_rel=media,
                          open_id=connection(cfg)["open_id"] or None)
        session.add(item)
        made += 1
    if made:
        session.commit()
    return made


def sync(session: Session, cfg: Optional[dict] = None, force: bool = False, in_worker: bool = False) -> dict:
    """Bring every active item up to date: renders (web process only), "posted yet?"
    for drafts, numbers for live clips. `force` (the page's Refresh) shortens the gaps.
    The network calls run with nothing pending in the session (SQLite write lock)."""
    from app.services.settings import load_settings
    from app.jobs.tasks import _apply_metrics
    from app.platforms.tiktok import VIDEO_FIELDS, metrics_of
    cfg = cfg if cfg is not None else load_settings()
    now = datetime.utcnow()
    out = {"renders": 0, "adopted": 0, "statuses": 0, "live": 0, "metrics": 0}
    items = session.exec(select(TikTokItem).where(TikTokItem.archived == False)).all()  # noqa: E712
    if not in_worker:
        for item in items:
            if _check_render(item):
                out["renders"] += 1
                _touch(session, item)
                _sync_upload_brief(session, item)
    conn = connection(cfg)
    if not conn["connected"]:
        return out
    out["adopted"] = adopt_factory_posts(session, cfg)
    if out["adopted"]:
        items = session.exec(select(TikTokItem).where(TikTokItem.archived == False)).all()  # noqa: E712
    client = _client(cfg)
    if client is None:
        return out
    acct = account(session, cfg, refresh=True, client=client,
                   max_age=timedelta(hours=2) if in_worker else timedelta(minutes=30))
    username = str(acct.get("username") or "")

    status_gap = timedelta(seconds=45) if force else timedelta(minutes=8)
    for item in items:
        post = session.get(Post, item.post_id) if item.post_id else None
        if post is None:
            continue
        if post.status == PostStatusEnum.awaiting_finish and not item.publish_id and post.platform_post_id:
            item.publish_id = post.platform_post_id
            _touch(session, item)
        if not item.publish_id or (item.open_id and conn["open_id"] and item.open_id != conn["open_id"]):
            continue
        waiting = post.status == PostStatusEnum.awaiting_finish and now - post.created_at < timedelta(days=30)
        no_id = (post.status == PostStatusEnum.done and not item.video_id and post.published_at is not None
                 and now - post.published_at < timedelta(days=7))
        if not (waiting or no_id):
            continue
        if item.status_checked_at and now - item.status_checked_at < status_gap:
            continue
        was_live = post.status == PostStatusEnum.done
        _poll_status(client, item, post, now, username)
        out["statuses"] += 1
        out["live"] += int(post.status == PostStatusEnum.done and not was_live)
        session.add(post)
        _touch(session, item)

    if conn["views"]:
        due = []
        for item in items:
            post = session.get(Post, item.post_id) if item.post_id else None
            if post is not None and item.video_id and post.status == PostStatusEnum.done \
                    and _metrics_due(post, now, force):
                due.append((item, post))
        if due:
            try:
                videos = {str(v.get("id")): v for v in client.query_videos([i.video_id for i, _ in due], VIDEO_FIELDS)}
            except Exception as exc:
                videos, error = None, str(exc)
            for item, post in due:
                video = videos.get(item.video_id) if videos is not None else None
                if video is None:
                    _apply_metrics(post, {"error": "tiktok_video_not_found" if videos is not None else error})
                else:
                    m = metrics_of(video)
                    _apply_metrics(post, m)
                    _push_history(item, post, m, now)
                    if video.get("share_url") and not post.platform_url:
                        post.platform_url = str(video["share_url"]).split("?", 1)[0]
                    out["metrics"] += 1
                session.add(post)
                _touch(session, item)
    return out


# ── the checklist ────────────────────────────────────────────────────────────

# key, stage, label ({lang} = the clip's language), filled in automatically
CHECKS = [
    ("screen_text", "prep", "On-screen text in {lang}", True),
    ("no_music", "prep", "No music baked in", True),
    ("length", "prep", "12–25 seconds", True),
    ("caption", "prep", "Caption in {lang}, no links", True),
    ("tags", "prep", "3–5 hashtags in {lang}", True),
    ("sound", "app", "Sound picked in the app", False),
    ("caption_pasted", "app", "Caption pasted", False),
    ("cover", "app", "Cover frame shows the hook text", False),
    ("posted_time", "app", "Posted 19:00–23:00 Japan time", True),
    ("replies", "after", "Answered the first comments within an hour", False),
    ("geo", "after", "Japan share typed in, after 2–3 days", True),
]
CHECK_KEYS = {c[0] for c in CHECKS}
HINTS = {
    "screen_text": "The text in the frame is the hook, and the strongest language signal TikTok reads.",
    "no_music": "Add the sound in the TikTok app. The API can’t attach native sounds, and a baked-in track "
                "can’t be turned down apart from the game audio.",
    "length": "The playbook aims for 12–25 s. Longer is fine when it holds the viewer.",
    "caption": "Links in TikTok captions aren’t clickable. Name the game and say it’s on Steam.",
    "tags": "For example: {hint}",
    "sound": "A track trending in Japan (Creative Center, region Japan), or keep the game audio. "
             "The app’s own trends list shows your region, not Japan.",
    "caption_pasted": "Drafts arrive without a caption: copy it here and paste it in the app.",
    "cover": "Pick a frame where the on-screen text reads at a glance.",
    "posted_time": "TikTok tests a clip on its first viewers within hours, so post when Japan is awake.",
    "replies": "Early replies keep the clip in the test pool longer.",
    "geo": "TikTok Studio → the video → Viewers → Locations. The API doesn’t give countries.",
}


def _ticks(item) -> dict:
    try:
        data = json.loads(item.checks_json or "{}")
    except ValueError:
        data = {}
    return {k: True for k, v in data.items() if v and k in CHECK_KEYS} if isinstance(data, dict) else {}


def _hashtags(tags: str) -> list[str]:
    return re.findall(r"#[^\s#,]+", tags or "")


def checks(item, post, info: dict, spec: Optional[dict]) -> list[dict]:
    """Every checklist row: state ok | warn | todo | na, a note, an optional fix action.
    A tick from the card turns any row that isn't ok into ok (checked by hand)."""
    name = LANG_NAMES.get(item.lang, item.lang)
    ticks = _ticks(item)
    st = stage(item, post)
    live = st == "live"
    rows = []
    for key, where, label, auto in CHECKS:
        if key == "posted_time" and item.lang != "ja":
            continue
        row = {"key": key, "stage": where, "label": label.format(lang=name), "auto": auto,
               "hint": HINTS[key].format(hint=TAG_HINTS.get(item.lang, "")), "state": "todo", "note": "",
               "action": None, "ticked": bool(ticks.get(key))}
        if key == "geo" and item.lang != "ja":
            row["label"] = "Top viewer country checked, after 2–3 days"
        if key == "screen_text":
            if spec is None:
                row["note"] = "Can’t see the text of this file. Check it yourself."
            else:
                texts = "\n".join(o.get("text") or "" for o in spec.get("overlays") or [])
                if not texts.strip():
                    row.update(state="warn", note="No text in the frame. Add a hook line with ✂ Edit.")
                elif repost.detect_lang(texts) != item.lang:
                    row.update(state="warn", note=f"The frame says: “{texts.splitlines()[0][:60]}”",
                               action="screen_text")
                else:
                    row.update(state="ok", note=texts.splitlines()[0][:60])
        elif key == "no_music":
            if spec is None:
                row["note"] = "A file made elsewhere: make sure it has no music in it."
            elif spec.get("music"):
                row.update(state="warn", note="A music track is baked in.", action="drop_music")
            else:
                row.update(state="ok", note="" if not info else "Game audio only" if info.get("has_audio") else
                           "Silent: the sound comes from the app")
        elif key == "length":
            dur = float(info.get("duration") or 0)
            if dur:
                lo, hi = LENGTH_RANGE
                row.update(state="ok" if lo <= dur <= hi else "warn", note=f"{dur:.0f} s")
            else:
                row["state"] = "na"
        elif key == "caption":
            text = item.caption or ""
            if not text.strip():
                row.update(state="warn", note="Empty")
            elif _LINK.search(text):
                row.update(state="warn", note="Has a link")
            elif repost.detect_lang(text) != item.lang:
                row.update(state="warn", note=f"Reads as {LANG_NAMES.get(repost.detect_lang(text) or '', 'something else')}")
            else:
                row["state"] = "ok"
        elif key == "tags":
            tags = _hashtags(item.tags)
            native = [t for t in tags if repost.detect_lang(t.lstrip("#")) in (item.lang, None)]
            if not 3 <= len(tags) <= 5:
                row.update(state="warn", note=f"{len(tags)} hashtags")
            elif item.lang == "ja" and not any(repost.detect_lang(t.lstrip("#")) == "ja" for t in tags):
                row.update(state="warn", note="None in Japanese")
            elif not native:
                row.update(state="warn", note=f"None in {name}")
            else:
                row.update(state="ok", note=f"{len(tags)} hashtags")
        elif key == "posted_time":
            when = post.published_at if (live and post is not None) else None
            if when is not None:
                jst = when.replace(tzinfo=timezone.utc).astimezone(JST)
                lo, hi = JP_EVENING
                row.update(state="ok" if lo <= jst.hour < hi else "warn",
                           note=f"{'≈' if not item.video_id else ''}{jst:%H:%M} in Japan")
        elif key == "geo":
            if item.jp_share is not None:
                row.update(state="ok", note=f"{item.jp_share:g}%")
            elif not live:
                row["state"] = "na"
        if row["ticked"] and row["state"] != "ok":
            row.update(state="ok", note=(row["note"] + " · " if row["note"] else "") + "checked by hand")
        if not auto:
            row["state"] = "ok" if row["ticked"] else "todo"
        rows.append(row)
    return rows


# ── what the page shows ──────────────────────────────────────────────────────

def _views_at(points: list, hours: float) -> Optional[int]:
    """Views `hours` after posting, interpolated; None until the clip is that old."""
    if not points or points[-1][0] < hours:
        return None
    for (h0, v0), (h1, v1) in zip(points, points[1:]):
        if h0 <= hours <= h1:
            return int(round(v0 + (v1 - v0) * ((hours - h0) / (h1 - h0) if h1 > h0 else 1)))
    return None


def _poster(brief) -> str:
    if not brief or not brief.media_path:
        return ""
    base = os.path.splitext(str(brief.media_path).replace("\\", "/").split("/")[-1])[0]
    return f"/media/{base}_poster.jpg" if os.path.exists(os.path.join("media", f"{base}_poster.jpg")) else ""


def _yt_views(session: Session, brief_ids: list[int]) -> dict:
    from app.services.publications import engagement
    out: dict = {}
    if not brief_ids:
        return out
    for p in session.exec(select(Post).where(Post.brief_id.in_(brief_ids), Post.platform == PlatformEnum.yt,
                                             Post.status == PostStatusEnum.done)).all():
        v = engagement(p.metrics_json)["views"]
        if v is not None:
            out[p.brief_id] = max(out.get(p.brief_id, 0), int(v))
    return out


def item_state(session: Session, item, yt_views: Optional[dict] = None, projects: Optional[dict] = None) -> dict:
    from app.services.publications import engagement, fmt_when
    post = session.get(Post, item.post_id) if item.post_id else None
    brief = session.get(Brief, item.brief_id)
    rel = _rel(item.video_rel) if not item.render_id else None
    info = file_info(rel) if rel else {}
    spec = item_spec(item) if rel else None
    st = stage(item, post)
    render = None
    if item.render_id:
        job = video_edit.get_job(item.render_id) or {}
        render = {"progress": job.get("progress") or 0.0, "status": job.get("status") or "running"}
        spec = job.get("spec")  # what the file will be
    metrics = None
    if post is not None and post.metrics_json:
        e = engagement(post.metrics_json)
        raw = json.loads(post.metrics_json or "{}") or {}
        metrics = {"views": e["views"], "likes": e["likes"], "comments": e["comments"], "shares": e["reposts"],
                   "error": e["error"], "stale": bool(raw.get("stale")),
                   "checked": fmt_when(_checked_at(post)) if _checked_at(post) else ""}
    points = []
    try:
        hist = json.loads(item.history_json or "[]")
    except ValueError:
        hist = []
    start = post.published_at if post is not None else None
    for p in hist:
        try:
            ts = datetime.fromisoformat(p[0])
        except (TypeError, ValueError, IndexError):
            continue
        if start is not None and p[1] is not None:
            points.append([round(max(0.0, (ts - start).total_seconds() / 3600), 2), int(p[1])])
    projects = projects if projects is not None else {}
    yt = (yt_views if yt_views is not None else _yt_views(session, [item.brief_id])).get(item.brief_id)
    return {
        "id": item.id, "brief_id": item.brief_id, "source": item.source, "lang": item.lang,
        "title": (brief.title if brief else "") or "", "project": projects.get(getattr(brief, "project_id", None), ""),
        "caption": item.caption or "", "tags": item.tags or "", "full_caption": full_caption(item),
        "caption_limit": CAPTION_LIMIT,
        "video_rel": rel or "", "video_url": f"/media/{rel}" if rel else "", "poster_url": _poster(brief),
        "duration": info.get("duration") or 0, "has_audio": bool(info.get("has_audio")),
        "editable": bool(spec is not None), "stage": st, "render": render, "render_error": item.render_error or "",
        "post": None if post is None else {
            "id": post.id, "status": getattr(post.status, "value", str(post.status)),
            "error": (post.error_text or "")[:300], "url": post.platform_url or "",
            "published": fmt_when(post.published_at) if post.published_at else "",
            "published_iso": post.published_at.isoformat(timespec="seconds") + "Z" if post.published_at else "",
            "sent": fmt_when(post.created_at) if post.created_at else "",
        },
        "exported": bool(post is not None and st == "inbox" and not item.publish_id and not post.platform_post_id),
        "tt_status": item.tt_status or "", "tt_error": item.tt_error or "",
        "status_checked": fmt_when(item.status_checked_at) if item.status_checked_at else "",
        "video_id": item.video_id or "", "metrics": metrics, "history": points,
        "views_24h": _views_at(points, 24), "views_1h": _views_at(points, 1),
        "jp_share": item.jp_share, "yt_views": yt,
        "checks": checks(item, post, info, spec),
    }


def candidates(session: Session, limit: int = 24) -> list[dict]:
    """Shorts with a render, the ones YouTube liked most first."""
    briefs = session.exec(select(Brief).where(Brief.vertical_916 == True, Brief.media_vertical_path.is_not(None))  # noqa: E712
                          .order_by(Brief.id.desc()).limit(300)).all()
    ids = [b.id for b in briefs]
    views = _yt_views(session, ids)
    in_pipe = {i.brief_id: i.id for i in session.exec(select(TikTokItem).where(TikTokItem.archived == False)).all()}  # noqa: E712
    names = {p.id: p.name for p in session.exec(select(Project)).all()}
    from app.services.publications import fmt_when
    rows = []
    for b in briefs:
        if not _rel(b.media_vertical_path):
            continue
        has_music = bool(brief_spec(b).get("music")) if (b.edit_spec_json or b.music_path) else False
        rows.append({"brief_id": b.id, "title": b.title or "", "views": views.get(b.id),
                     "project": names.get(b.project_id, ""), "poster_url": _poster(b),
                     "video_url": f"/media/{_rel(b.media_vertical_path)}", "has_music": has_music,
                     "created": fmt_when(b.created_at) if b.created_at else "", "item_id": in_pipe.get(b.id)})
    rows.sort(key=lambda r: (r["views"] is not None, r["views"] or 0, r["brief_id"]), reverse=True)
    return rows[:limit]


def summary(states: list[dict], follower: dict) -> dict:
    live = [s for s in states if s["stage"] == "live"]
    views = [s["metrics"]["views"] for s in live if s.get("metrics") and isinstance(s["metrics"].get("views"), int)]
    shares = [s["jp_share"] for s in live if s.get("jp_share") is not None and s["lang"] == "ja"]
    first = next((s["post"]["published_iso"] for s in sorted(live, key=lambda s: s["post"]["published_iso"])
                  if s["post"]["published_iso"]), "")
    vals, days = follower.get("values") or [], follower.get("days") or []
    gained = None
    if vals and first:
        before = [v for d, v in zip(days, vals) if d < first[:10]]
        base = before[-1] if before else vals[0]
        gained = vals[-1] - base
    return {
        "live": len(live), "inbox": sum(s["stage"] == "inbox" for s in states),
        "ready": sum(s["stage"] in ("ready", "rendering") for s in states),
        "views_total": sum(views) if views else None,
        "views_median": int(statistics.median(views)) if views else None,
        "jp_avg": round(sum(shares) / len(shares), 1) if shares else None, "jp_n": len(shares),
        "jp_needed": SHARE_SAMPLE, "target_share": TARGET_SHARE,
        "followers_gained": gained,
    }


def state(session: Session, cfg: dict) -> dict:
    """Everything the page draws, JSON-ready (no network)."""
    items = session.exec(select(TikTokItem).where(TikTokItem.archived == False)  # noqa: E712
                         .order_by(TikTokItem.id.desc())).all()
    for item in items:  # finished renders (an in-memory look-up, no network)
        if item.render_id and _check_render(item):
            _touch(session, item)
            _sync_upload_brief(session, item)
    projects = {p.id: p.name for p in session.exec(select(Project)).all()}
    yt = _yt_views(session, [i.brief_id for i in items])
    states = [item_state(session, i, yt, projects) for i in items]
    acct = account(session, cfg)
    follower = follower_series(session)
    if acct.get("follower_count") is not None and not follower["values"]:
        follower = {"days": [datetime.utcnow().date().isoformat()], "values": [acct["follower_count"]]}
    return {
        "connection": connection(cfg), "account": acct, "followers": follower,
        "items": states, "summary": summary(states, follower),
    }


def page_context(session: Session, cfg: dict) -> dict:
    projects = [{"id": p.id, "name": p.name} for p in session.exec(select(Project).order_by(Project.sort_order,
                                                                                            Project.id)).all()
                if (p.status or "") != "archived"]
    return {
        **state(session, cfg),
        "candidates": candidates(session),
        "projects": projects,
        "langs": [{"key": k, "name": LANG_NAMES[k]} for k in LANGS],
        "jp_evening": list(JP_EVENING),
    }
