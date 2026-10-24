"""Thumbnails of a Brief's main media for the post tables (Analytics, Posts, Jobs, Calendar).

An image is its own thumbnail. A video can't go into an <img>: it gets a poster frame
`media/<name>_poster.jpg` — the Shorts render writes one, any other video gets it on the
first request to /api/media/poster/{brief_id}, so a table render never waits for ffmpeg.
"""
from __future__ import annotations

import os
import threading
from typing import Optional

MEDIA_DIR = "media"
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_failed: set[str] = set()     # sources ffmpeg couldn't read: not retried on every page load


def _name(rel) -> str:
    return str(rel or "").replace("\\", "/").split("/")[-1]


def is_video(rel) -> bool:
    return os.path.splitext(_name(rel))[1].lower() in VIDEO_EXTS


def poster_name(rel) -> str:
    """Poster file of a video, named after the Brief's media_path (as render_short does)."""
    return f"{os.path.splitext(_name(rel))[0]}_poster.jpg"


def thumb_url(brief) -> Optional[str]:
    """URL for an <img> of the brief's main media, or None when it has none."""
    rel = getattr(brief, "media_path", None) if brief is not None else None
    if not rel:
        return None
    if not is_video(rel):
        return f"/media/{_name(rel)}"
    poster = poster_name(rel)
    if os.path.exists(os.path.join(MEDIA_DIR, poster)):
        return f"/media/{poster}"
    return f"/api/media/poster/{brief.id}"


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def ensure_poster(brief) -> Optional[str]:
    """Path of the brief's video poster, grabbing the frame on first use; None when
    the brief has no video or no frame could be read."""
    rel = getattr(brief, "media_path", None)
    if not rel or not is_video(rel):
        return None
    out = os.path.join(MEDIA_DIR, poster_name(rel))
    if os.path.exists(out):
        return out
    # the rendered 9:16 clip is what went out; the source is the fallback
    sources = [os.path.join(MEDIA_DIR, _name(r)) for r in (brief.media_vertical_path, rel) if r and is_video(r)]
    sources = [s for s in sources if os.path.exists(s) and s not in _failed]
    if not sources:
        return None
    from app.services.video_editor import extract_poster_frame
    with _lock(out):
        if os.path.exists(out):
            return out
        tmp = out[:-len(".jpg")] + ".tmp.jpg"
        for src in sources:
            if extract_poster_frame(src, tmp, at_sec=1.0):
                os.replace(tmp, out)
                return out
            _failed.add(src)
        if os.path.exists(tmp):
            os.remove(tmp)
    return None
