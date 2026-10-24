"""Publications (a Brief and its per-platform posts): engagement and follower gains.

Engagement comes from posts.metrics_json (kept fresh by the metrics tasks); follower
counts from audience_samples, read every 2 hours and right before a post goes out
(marketing_collect.snapshot_before_publish). A publication "went out" at its first
published post; "before" is the last read up to that moment.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import delete
from sqlmodel import Session, select

from app.models.models import Brief, MetricPoint, Post, PostStatusEnum, Project
from app.services import audience
from app.services import marketing as mk

PLATFORM_LABELS = {
    "vk": "VK", "vk_video": "VK Video", "x": "X", "yt": "YouTube", "discord": "Discord",
    "tiktok": "TikTok", "fb_ru": "Facebook RU", "fb_en": "Facebook EN", "reddit": "Reddit",
    "tg": "Telegram", "bluesky": "Bluesky", "ig_reels": "Instagram",
}
CHECKPOINTS = [("+24 h", 24), ("+3 days", 72), ("+7 days", 168)]
WINDOW_H = 72                            # "followers gained" in lists = the first 3 days
BEFORE_MAX_AGE = timedelta(hours=36)     # an older read doesn't count as "before the post"
USUAL_DAYS = 14                          # the usual growth rate is measured over this
POST_METRIC_DAYS = 120                   # post_views / post_reactions are rebuilt this far back
POST_METRICS = ("post_views", "post_reactions")


def platform_label(key: str) -> str:
    return PLATFORM_LABELS.get(key, key)


def _key(platform) -> str:
    from app.platforms import platform_key
    return platform_key(platform)


def engagement(raw) -> dict:
    """Counters of one post, whatever each platform calls them. reactions = likes +
    reposts + comments + quotes; views are reach, not a reaction."""
    m = raw if isinstance(raw, dict) else {}
    if isinstance(raw, str) and raw:
        try:
            m = json.loads(raw) or {}
        except ValueError:
            m = {}

    def pick(*keys):
        for k in keys:
            v = m.get(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
        return None
    out = {"likes": pick("like", "likes"), "reposts": pick("retweet", "reposts", "repost"),
           "comments": pick("reply", "comments"), "quotes": pick("quote"), "views": pick("views", "impressions")}
    nums = [out[k] for k in ("likes", "reposts", "comments", "quotes") if out[k] is not None]
    out["reactions"] = sum(nums) if nums else None
    out["has"] = bool(nums) or out["views"] is not None
    out["error"] = m.get("error")
    return out


TOTAL_KEYS = ("views", "likes", "reposts", "comments", "reactions")


def empty_total() -> dict:
    return dict.fromkeys(TOTAL_KEYS)


def add_total(total: dict, e: dict) -> None:
    """Sum counters into `total`; a key stays None until some post reports it."""
    for k in TOTAL_KEYS:
        if e.get(k) is not None:
            total[k] = (total.get(k) or 0) + e[k]


def to_local(ts: datetime) -> datetime:
    """Naive UTC (published_at, sample ts) → naive local time."""
    return ts.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def local_day(ts: datetime) -> str:
    return to_local(ts).date().isoformat()


def fmt_elapsed(delta: timedelta) -> str:
    """'27 min', '5 h', '2 d 4 h'."""
    minutes = max(0, int(delta.total_seconds() // 60))
    if minutes < 60:
        return f"{minutes} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} d" + (f" {hours % 24} h" if hours % 24 else "")


def fmt_when(ts: Optional[datetime]) -> str:
    """'Sep 29, 18:05' in local time ('Sep 29, 2025, 18:05' in another year),
    for a naive UTC timestamp."""
    if not ts:
        return "—"
    loc = to_local(ts)
    day = f"{loc:%b} {loc.day}" + ("" if loc.year == datetime.now().year else f", {loc.year}")
    return f"{day}, {loc:%H:%M}"


# ── daily views/reactions for tiles and goals ────────────────────────────────

def rebuild_post_metrics(session: Session) -> str:
    """post_views / post_reactions per day of publication, for all posts (project 0)
    and per project. Counters keep growing after the day, so the window is rebuilt."""
    first_day = mk.shift(mk.today_str(), -POST_METRIC_DAYS)
    since = datetime.utcnow() - timedelta(days=POST_METRIC_DAYS + 1)
    rows = session.exec(select(Post, Brief.project_id).where(
        Post.brief_id == Brief.id, Post.status == PostStatusEnum.done,
        Post.published_at.is_not(None), Post.published_at >= since)).all()
    sums: dict[tuple[int, str, str], float] = {}
    briefs = set()
    for post, pid in rows:
        day = local_day(post.published_at)
        if day < first_day:
            continue
        briefs.add(post.brief_id)
        e = engagement(post.metrics_json)
        for owner in {0, int(pid or 0)}:
            if e["views"] is not None:
                sums[(owner, "post_views", day)] = sums.get((owner, "post_views", day), 0) + e["views"]
            sums[(owner, "post_reactions", day)] = sums.get((owner, "post_reactions", day), 0) + (e["reactions"] or 0)
    session.execute(delete(MetricPoint).where(MetricPoint.metric.in_(POST_METRICS), MetricPoint.day >= first_day))
    mk.upsert_points(session, [{"project_id": pid, "metric": metric, "day": day, "value": v, "source": "posts"}
                               for (pid, metric, day), v in sums.items()])  # commits the delete too
    return f"posts in the last {POST_METRIC_DAYS} d: {len(briefs)}"


# ── publications ─────────────────────────────────────────────────────────────

def published(session: Session, since: datetime, until: Optional[datetime] = None,
              project_id: Optional[int] = None) -> list[dict]:
    """Publications that first went out in [since, until], newest first, with their
    published posts and summed counters."""
    q = select(Post, Brief).where(Post.brief_id == Brief.id, Post.status == PostStatusEnum.done,
                                  Post.published_at.is_not(None),
                                  Post.published_at >= since - timedelta(days=7))
    if until is not None:
        q = q.where(Post.published_at <= until + timedelta(days=7))
    if project_id:
        q = q.where(Brief.project_id == project_id)
    by_brief: dict[int, dict] = {}
    for post, brief in session.exec(q).all():
        pub = by_brief.setdefault(brief.id, {"brief": brief, "posts": [], "t0": post.published_at,
                                             "platforms": [], "errors": 0})
        pub["posts"].append(post)
        pub["t0"] = min(pub["t0"], post.published_at)
        key = _key(post.platform)
        if key not in pub["platforms"]:
            pub["platforms"].append(key)
    out = []
    for pub in by_brief.values():
        if pub["t0"] < since or (until is not None and pub["t0"] > until):
            continue
        total = empty_total()
        for post in pub["posts"]:
            e = engagement(post.metrics_json)
            add_total(total, e)
            pub["errors"] += bool(e["error"])
        pub.update(total=total, title=mk._brief_title(pub["brief"]), day=local_day(pub["t0"]),
                   platform_labels=[platform_label(k) for k in sorted(pub["platforms"], key=platform_label)])
        out.append(pub)
    out.sort(key=lambda p: p["t0"], reverse=True)
    return out


def _gain(s: mk.Samples, metric: str, pid: int, t0: datetime, end: datetime) -> Optional[float]:
    before = s.at(metric, t0, pid, not_before=t0 - BEFORE_MAX_AGE)
    after = s.at(metric, end, pid, not_before=t0)
    return after[1] - before[1] if before and after else None


def follower_gain(s: mk.Samples, pub: dict, now: datetime, hours: int = WINDOW_H,
                  chans: Optional[list] = None) -> dict:
    """Followers gained in the first `hours` after a publication (or so far): on the
    accounts it went to, and on all of them. None where there's no read before/after."""
    t0 = pub["t0"]
    end = min(now, t0 + timedelta(hours=hours))
    chans = chans if chans is not None else audience.channels()
    went = audience.reached(chans, pub["posts"])
    posted = every = None
    counted, missing = [], []
    for ch in chans:
        here = ch.key in went
        g = _gain(s, ch.metric, 0, t0, end)
        if g is None:
            if here:
                missing.append(ch.label)
            continue
        every = (every or 0) + g
        if here:
            posted = (posted or 0) + g
            counted.append(ch.label)
    pid = int(pub["brief"].project_id or 0)
    steam = _gain(s, "steam_followers", pid, t0, end) if pid else None
    return {"posted": posted, "all": every, "steam": steam, "complete": now >= t0 + timedelta(hours=hours),
            "elapsed": fmt_elapsed(end - t0), "counted": counted, "missing": missing}


def with_gains(session: Session, pubs: list[dict], now: Optional[datetime] = None) -> list[dict]:
    now = now or datetime.utcnow()
    if pubs:
        s = mk.Samples(session, min(p["t0"] for p in pubs) - BEFORE_MAX_AGE)
        chans = audience.channels()
        for p in pubs:
            p["gain"] = follower_gain(s, p, now, chans=chans)
    return pubs


def report(session: Session, brief: Brief, now: Optional[datetime] = None) -> dict:
    """Everything the publication page shows: per-post counters, followers before →
    +24 h / +3 days / +7 days → now per account, and what else went out meanwhile."""
    now = now or datetime.utcnow()
    posts = session.exec(select(Post).where(Post.brief_id == brief.id).order_by(Post.id)).all()
    project = session.get(Project, brief.project_id) if brief.project_id else None
    total = empty_total()
    post_rows = []
    for p in posts:
        e = engagement(p.metrics_json)
        if p.status == PostStatusEnum.done:
            add_total(total, e)
        key = _key(p.platform)
        post_rows.append({"post": p, "key": key, "label": platform_label(key), "e": e,
                          "status": p.status.value if hasattr(p.status, "value") else str(p.status)})
    done = [p for p in posts if p.status == PostStatusEnum.done and p.published_at]
    ctx = {"brief": brief, "project": project, "posts": post_rows, "total": total,
           "title": mk._brief_title(brief), "t0": None, "rows": [], "others": [], "checkpoints": CHECKPOINTS}
    if not done:
        planned = [p.scheduled_at for p in posts if p.status == PostStatusEnum.queued and p.scheduled_at]
        ctx["planned"] = min(planned) if planned else None
        return ctx
    t0 = min(p.published_at for p in done)
    platforms = {_key(p.platform) for p in done}
    s = mk.Samples(session, t0 - timedelta(days=USUAL_DAYS + 2))
    chans = audience.channels()
    went = audience.reached(chans, done)  # each destination counted on its own is its own account
    targets = [(ch.label, ch.metric, 0, ch.key in went) for ch in chans]
    if project and project.steam_app_id:
        targets.append((f"Steam: {project.name}", "steam_followers", project.id, True))
    end = min(now, t0 + timedelta(hours=WINDOW_H))
    rows = []
    for label, metric, pid, posted in targets:
        before = s.at(metric, t0, pid, not_before=t0 - BEFORE_MAX_AGE)
        latest = s.latest(metric, pid)
        latest = latest if latest and latest[0] >= t0 else None
        if before is None and latest is None:
            continue
        cps = []
        for name, hours in CHECKPOINTS:
            at_t = t0 + timedelta(hours=hours)
            if at_t > now:
                cps.append({"label": name, "pending": True})
                continue
            got = s.at(metric, at_t, pid, not_before=t0)
            cps.append({"label": name, "value": got[1] if got else None,
                        "delta": got[1] - before[1] if got and before else None})
        usual = None
        prior = s.at(metric, t0 - timedelta(days=USUAL_DAYS), pid) or s.first(metric, pid, t0 - timedelta(days=USUAL_DAYS))
        if before and prior and (before[0] - prior[0]) >= timedelta(days=3):
            usual = (before[1] - prior[1]) / ((before[0] - prior[0]).total_seconds() / 86400)
        rows.append({
            "label": label, "metric": metric, "posted": posted, "steam": bool(pid),
            "before": before[1] if before else None, "before_ts": before[0] if before else None,
            "checkpoints": cps, "now": latest[1] if latest else None,
            "now_delta": latest[1] - before[1] if latest and before else None,
            "window_delta": _gain(s, metric, pid, t0, end),
            "usual_per_day": usual,
        })
    rows.sort(key=lambda r: (not r["posted"], r["steam"], -(r["now"] or r["before"] or 0)))
    social = [r for r in rows if not r["steam"] and r["window_delta"] is not None]
    posted_labels = [ch.label for ch in chans if ch.key in went]
    counted = [r["label"] for r in social if r["posted"]]
    others = [p for p in published(session, t0 - timedelta(hours=24), t0 + timedelta(hours=WINDOW_H))
              if p["brief"].id != brief.id]
    ctx.update(
        t0=t0, rows=rows, others=others,
        platform_labels=sorted(platform_label(k) for k in platforms),
        gain_posted=sum(r["window_delta"] for r in social if r["posted"]) if any(r["posted"] for r in social) else None,
        gain_all=sum(r["window_delta"] for r in social) if social else None,
        gain_steam=next((r["window_delta"] for r in rows if r["steam"]), None),
        window_elapsed=fmt_elapsed(end - t0), window_complete=now >= t0 + timedelta(hours=WINDOW_H),
        has_before=any(r["before"] is not None for r in rows),
        gain_counted=counted, gain_missing=[label for label in posted_labels if label not in counted],
    )
    return ctx
