"""Never send the same publication to a network twice.

On 2026-09-30 a server restart made the open "post created" tab reload, the browser
re-submitted the Create Post form and every network got the post a second time.
The forms now redirect after POST, and on top of that:

- the worker refuses to publish a post when the same platform already got one with
  the same text and media within WINDOW (any path: forms, Shorts, API, retries);
- the forms refuse to create such a publication unless "publish again" is ticked;
- Retry in Jobs on a post cancelled here is the explicit "publish anyway".
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlmodel import select

WINDOW = timedelta(days=7)
MARK = "duplicate_of_post"
# the payload / form flag that lets a deliberate repeat through
ALLOW = "allow_duplicate"


# uploads are stored as "<unix time>_<original name>": the same file uploaded twice matches
_UPLOAD_STAMP = re.compile(r"^\d{9,11}_")


def _media_name(path: Optional[str]) -> str:
    return _UPLOAD_STAMP.sub("", (path or "").replace("\\", "/").rsplit("/", 1)[-1].strip())


def fingerprint(brief) -> Optional[str]:
    """Same texts (whitespace-insensitive) and same media files = the same publication.
    Tags stay out: weekday auto-tags differ between two submissions of one post."""
    if brief is None:
        return None
    texts = [" ".join((getattr(brief, f, None) or "").split()) for f in ("text_ru", "text_en")]
    # the other languages count too, only when there are any: a RU/EN brief keeps the fingerprint it had
    from app.services import languages
    extra = languages.brief_texts(brief)
    texts += [f"{lang}:{' '.join(extra[lang].split())}" for lang in sorted(extra)
              if lang not in languages.MIRRORED and extra[lang].strip()]
    media = sorted({_media_name(getattr(brief, f, None))
                    for f in ("media_path", "media_normal_path", "media_vertical_path")} - {""})
    if not any(texts) and not media:
        return None  # nothing to compare an empty brief by
    return hashlib.sha1("\x1f".join(texts + media).encode("utf-8")).hexdigest()


def _same_place(post, destination_id) -> bool:
    """Two posts on one platform went to the same place unless both name a destination and they differ:
    a post without one went to the platform's original target, which its migrated destination still is."""
    other = getattr(post, "destination_id", None)
    return not (other and destination_id) or other == destination_id


def _twins(session, fp: str, platforms: Iterable, statuses: Iterable, exclude_post_id=None, destinations=None):
    """`destinations`: {platform key: [destination ids]} — when given, a post on such a platform is a twin
    only if it went to one of those destinations (or to the platform's original target)."""
    from app.models.models import Brief, Post
    from app.platforms import platform_key
    since = datetime.utcnow() - WINDOW
    q = (select(Post, Brief).join(Brief, Post.brief_id == Brief.id)
         .where(Post.platform.in_(list(platforms)), Post.status.in_(list(statuses)), Post.created_at >= since)
         .order_by(Post.id))
    for post, brief in session.exec(q).all():
        if post.id == exclude_post_id or fingerprint(brief) != fp:
            continue
        wanted = (destinations or {}).get(platform_key(post.platform))
        if wanted is not None and not any(_same_place(post, d) for d in wanted):
            continue
        yield post


def published_twin(session, post):
    """An earlier post of the same publication on the same platform (and destination) that is
    live or being published right now, or None."""
    from app.models.models import Brief, PostStatusEnum as S
    from app.platforms import platform_key
    fp = fingerprint(session.get(Brief, post.brief_id))
    if fp is None:
        return None
    live = (S.done, S.running, S.awaiting_finish)
    dest = getattr(post, "destination_id", None)
    return next(_twins(session, fp, [post.platform], live, exclude_post_id=post.id,
                       destinations={platform_key(post.platform): [dest]} if dest else None), None)


def existing_publication(session, brief, platforms: Iterable, destinations: Optional[dict] = None):
    """For a brief about to be created: a recent post of the same publication on one of
    `platforms` that went out or is still waiting to (queued counts: a double submit).
    `destinations` ({platform key: [destination ids]}) narrows it to the chosen destinations."""
    from app.models.models import PostStatusEnum as S
    fp = fingerprint(brief)
    platforms = list(platforms or [])
    if fp is None or not platforms:
        return None
    pending_or_live = (S.queued, S.running, S.done, S.awaiting_finish)
    return next(_twins(session, fp, platforms, pending_or_live, destinations=destinations), None)


def reason(twin) -> str:
    from app.platforms import platform_key
    when = twin.published_at or twin.created_at
    where = platform_key(twin.platform) + (f" ({twin.destination_id})" if getattr(twin, "destination_id", None) else "")
    return (f"{MARK} #{twin.id}: the same text and media already went to {where} "
            f"({when:%Y-%m-%d %H:%M} UTC) — not publishing it twice. Retry in Jobs publishes it anyway.")
