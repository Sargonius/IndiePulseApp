"""X API is pay-per-use: prepaid credits in console.x.com, every call is billed.

When the balance hits zero X answers HTTP 402 "credits depleted" to everything —
tweets fail, stats and follower counts freeze. Every X call site reports its
answer here (`remember`), which keeps one flag in settings; the page banner, the
Accounts → X guide, the tray and /status read it. The first successful call after
a top-up clears it.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from time import time as _now

from app.services.settings import load_settings, save_settings

CONSOLE_URL = "https://console.x.com"
PRICING_URL = "https://docs.x.com/x-api/getting-started/pricing"
DEPLETED = "x_credits_depleted"
ERROR_TEXT = f"{DEPLETED}: out of X API credits (HTTP 402) — top up your balance at console.x.com"

# USD per billed request, docs.x.com pricing as of 2026-09. A resource is billed
# once per UTC day, so the 30-min stats refresh costs one read a day per tweet.
PRICE_POST = 0.015
PRICE_POST_URL = 0.20   # any http(s) link in the text, incl. the expanded {steam} link
PRICE_OWNED_READ = 0.001
METRIC_DAYS = 7          # refresh_recent_metrics reads a tweet for a week after publish

_URL = re.compile(r"https?://", re.I)


def is_depleted(status: int, body) -> bool:
    text = str(body or "").lower()
    return status == 402 or "credits depleted" in text or "credits-depleted" in text


def remember(status: int, body=None, billed: bool = True) -> bool:
    """Record (402) or clear (2xx) the 'credits ran out' flag. True = depleted.
    billed=False for users/me: X answers it even on an empty balance (seen 2026-09-30:
    tweet reads 402, users/me 200), so its success proves nothing — only a 402 counts.
    Writes settings only when the state changes — callers hit this on every request."""
    depleted = is_depleted(status, body)
    if not depleted and not (billed and 200 <= status < 300):
        return False  # another error (or a free endpoint) says nothing about the balance
    try:
        since = int(load_settings().get("x_credits_error_at") or 0)
        if depleted and not since:
            save_settings({"x_credits_error_at": int(_now())})
        elif not depleted and since:
            save_settings({"x_credits_error_at": 0})
    except Exception:
        pass  # bookkeeping must never break a publish
    return depleted


def depleted_since(cfg: dict) -> int:
    return int(cfg.get("x_credits_error_at") or 0)


def _latest_tweet_id() -> str:
    from sqlmodel import Session, select
    from app.db.session import engine
    from app.models.models import Post
    from app.platforms import platform_key
    with Session(engine) as session:
        rows = session.exec(select(Post).where(Post.platform_post_id.is_not(None))
                            .order_by(Post.published_at.desc(), Post.id.desc()).limit(200)).all()
    return next((p.platform_post_id for p in rows if platform_key(p.platform) == "x"), "")


def probe(token: str = "") -> tuple[bool | None, str]:
    """Is there money on the balance? (depleted, note); None = couldn't tell.
    Reads our latest tweet: a billed read (≤ $0.005, free if already read this UTC
    day), unlike users/me which X serves regardless."""
    import httpx
    tid = _latest_tweet_id()
    if not tid:
        return None, "nothing to check the balance with: IndiePulse hasn’t published any tweets yet"
    try:
        if not token:
            from app.services.tokens import x_ensure_token
            token = x_ensure_token()
        with httpx.Client(timeout=15) as client:
            r = client.get("https://api.twitter.com/2/tweets", params={"ids": tid},
                           headers={"Authorization": f"Bearer {token}"})
    except Exception as exc:
        return None, f"couldn’t check: {exc}"
    if remember(r.status_code, r.text):
        return True, "X still answers 402 — if you just topped up, wait a couple of minutes."
    if r.status_code >= 400:
        return None, f"X answered HTTP {r.status_code}: {r.text[:150]}"
    return False, ""


def failed_posts(session, days: int = 14) -> int:
    """Recent X posts that failed on the empty balance and still wait for a retry
    (older ones — e.g. the April 2026 outage — are news nobody will resend)."""
    from sqlmodel import or_, select
    from app.models.models import Post, PostStatusEnum
    from app.platforms import platform_key
    rows = session.exec(select(Post).where(
        Post.status == PostStatusEnum.error,
        Post.updated_at >= datetime.utcnow() - timedelta(days=days),
        # 'status=402': posts that failed before this module named the error
        or_(Post.error_text.contains(DEPLETED), Post.error_text.contains("status=402")))).all()
    return sum(1 for p in rows if platform_key(p.platform) == "x")


def alert(cfg: dict) -> dict | None:
    """Banner text while the balance is empty (settings only, no network)."""
    since = depleted_since(cfg)
    if not since:
        return None
    from app.services.youtube_setup import fmt_when
    return {"text": f"X API: out of credits (since {fmt_when(since)}) — posts to X don’t go out, "
                    "X stats and followers aren’t updating."}


def tray_reminder(cfg: dict) -> tuple[str, str] | None:
    """(key, text) of a one-off Windows notification per outage."""
    since = depleted_since(cfg)
    if not since:
        return None
    return (f"x-credits:{since}", "X API: out of credits — posts to X don’t go out. "
                                  "Top up your balance at console.x.com (Billing).")


def _tweets(session, post) -> list[str]:
    """The tweets this post went out as: one, or the thread parts."""
    import json as _json
    from app.jobs.tasks import find_publish_jobs
    from app.platforms import x as xp
    from app.platforms.common import brief_for_publish
    base, tags = xp.compose(brief_for_publish(session, post), post)
    full = (base + ("\n" + tags if tags else "")).strip()
    if len(full) <= 280:
        return [full]
    for job in find_publish_jobs(session, post.id)[:1]:
        try:
            thread = (_json.loads(job.payload_json or "{}") or {}).get("x_thread") is True
        except Exception:
            thread = False
        if thread or "x_280_limit_fallback_thread" in (job.last_error or ""):
            return xp.thread_parts(base, tags)
    return [full]  # went out whole (X Premium)


def usage(session, days: int = 30) -> dict:
    """What IndiePulse spent on X over the last `days`, at list prices."""
    from sqlmodel import select
    from app.models.models import Post, PostStatusEnum
    from app.platforms import platform_key
    since = datetime.utcnow() - timedelta(days=days)
    rows = session.exec(select(Post).where(Post.status == PostStatusEnum.done,
                                           Post.published_at >= since)).all()
    posts = tweets = with_url = 0
    for post in rows:
        if platform_key(post.platform) != "x":
            continue
        posts += 1
        for text in _tweets(session, post):
            tweets += 1
            with_url += bool(_URL.search(text))
    tweet_cost = (tweets - with_url) * PRICE_POST + with_url * PRICE_POST_URL
    # stats: one billed read a day per tweet for a week; followers: one a day
    read_cost = (posts * METRIC_DAYS + days) * PRICE_OWNED_READ
    return {"days": days, "posts": posts, "tweets": tweets, "with_url": with_url,
            "tweet_cost": tweet_cost, "read_cost": read_cost, "total": tweet_cost + read_cost}
