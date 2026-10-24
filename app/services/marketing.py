"""Marketing layer: metric registry, daily snapshots, goals.

Every metric is stored as one value per (project, metric, dim, day) in
`metric_points` (project 0 = account-wide). Two kinds:

* flow  — an amount per day (wishlist adds, units sold, page visits). Progress over
  a window is the sum; a manual weekly entry is spread evenly over its days.
* level — a counter (followers of each social account, Steam page followers).
  Progress is the change since the goal started, or the current value for "reach N"
  goals.

Follower counts are also kept with their exact time in `audience_samples`, for the
before/after numbers of a publication (publications.py). Collectors that fill both
live in marketing_collect.py.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable, Optional

from sqlmodel import Session, select

from app.models.models import (Activity, AudienceSample, Brief, Goal, GoalLink, KVState, MetricPoint,
                               Post, PostStatusEnum, Project, now_ts)

# Lifecycle of a game on Steam, in order. Used for the badge on the home screen.
PROJECT_STAGES: dict[str, str] = {
    "prototype": "Prototype",
    "no_page": "Preparing page",
    "coming_soon": "Coming Soon",
    "demo": "Demo",
    "early_access": "Early Access",
    "released": "Released",
}
# None/"" = active; paused projects collapse into one row on the home screen.
PROJECT_STATUSES: dict[str, str] = {"active": "Active", "paused": "Paused", "archived": "Archived"}

METRICS: dict[str, dict] = {
    "steam_wishlist_adds": {"label": "Wishlist adds", "kind": "flow", "scope": "project"},
    "steam_wishlist_net": {"label": "Wishlist net adds", "kind": "flow", "scope": "project",
                           "hint": "adds − deletes"},
    "steam_wishlist_deletes": {"label": "Wishlist deletes", "kind": "flow", "scope": "project"},
    "steam_wishlist_purchases": {"label": "Wishlist purchases", "kind": "flow", "scope": "project"},
    "steam_wishlist_gifts": {"label": "Wishlist gifts", "kind": "flow", "scope": "project"},
    "steam_wishlist_balance": {"label": "Total wishlists", "kind": "level", "scope": "project",
                               "hint": "computed from API history since the page went live"},
    "steam_units": {"label": "Sales, units", "kind": "flow", "scope": "project"},
    "steam_revenue_usd": {"label": "Revenue (net), $", "kind": "flow", "scope": "project", "unit": "$"},
    "steam_visits": {"label": "Steam page visits", "kind": "flow", "scope": "project", "manual": True,
                     "hint": "bot traffic excluded; imported via the “IndiePulse ← Steam” bookmarklet"},
    "steam_impressions": {"label": "Steam impressions", "kind": "flow", "scope": "project", "manual": True},
    "steam_followers": {"label": "Steam: page followers", "kind": "level", "scope": "project",
                        "hint": "followers of the game’s Steam community"},
    # scope "any": a goal can be about one project's publications or about all of them
    "post_views": {"label": "Posts: views", "kind": "flow", "scope": "any",
                   "hint": "by publish day: views of posts that went out on those days"},
    "post_reactions": {"label": "Posts: reactions", "kind": "flow", "scope": "any",
                       "hint": "likes + comments + reposts, by the post’s publish day"},
    "followers_total": {"label": "Followers: all networks", "kind": "level", "scope": "global",
                        "hint": "sum over connected networks; connecting a new one isn’t growth"},
    "discord_members": {"label": "Discord: members", "kind": "level", "scope": "global"},
    "yt_subscribers": {"label": "YouTube: subscribers", "kind": "level", "scope": "global"},
    "yt_views": {"label": "YouTube: channel views", "kind": "level", "scope": "global"},
    "vk_members": {"label": "VK: followers", "kind": "level", "scope": "global"},
    "x_followers": {"label": "X: followers", "kind": "level", "scope": "global"},
    "tg_subscribers": {"label": "Telegram: subscribers", "kind": "level", "scope": "global"},
    "fb_ru_followers": {"label": "Facebook RU: followers", "kind": "level", "scope": "global"},
    "fb_en_followers": {"label": "Facebook EN: followers", "kind": "level", "scope": "global"},
    "bsky_followers": {"label": "Bluesky: followers", "kind": "level", "scope": "global"},
    "reddit_subscribers": {"label": "Reddit: subreddit subscribers", "kind": "level", "scope": "global"},
    "ig_followers": {"label": "Instagram: followers", "kind": "level", "scope": "global"},
    "tiktok_followers": {"label": "TikTok: followers", "kind": "level", "scope": "global"},
}


# The followers of a destination counted on its own (app.services.audience.DESTINATION_METRICS):
# "<the network's metric>:<destination id>", a level like the network's own.
_DESTINATION_FOLLOWERS = {"tg_subscribers": "Telegram", "fb_followers": "Facebook", "reddit_subscribers": "Reddit"}


def _spec(metric: str) -> dict:
    spec = METRICS.get(metric)
    if spec is None and ":" in str(metric):
        base, dest = str(metric).split(":", 1)
        if base in _DESTINATION_FOLLOWERS:
            spec = {"label": f"{_DESTINATION_FOLLOWERS[base]} {dest}: followers", "kind": "level", "scope": "global"}
    return spec or {}


def metric_kind(metric: str) -> str:
    return _spec(metric).get("kind", "flow")


def metric_label(metric: str) -> str:
    return _spec(metric).get("label", metric)


# ── dates ────────────────────────────────────────────────────────────────────

def today_str() -> str:
    return date.today().isoformat()


def to_date(day: str) -> date:
    return date.fromisoformat(str(day)[:10])


def shift(day: str, days: int) -> str:
    return (to_date(day) + timedelta(days=days)).isoformat()


def day_range(start: str, end: str) -> list[str]:
    a, b = to_date(start), to_date(end)
    return [(a + timedelta(days=i)).isoformat() for i in range((b - a).days + 1)] if b >= a else []


def days_between(start: str, end: str) -> int:
    return (to_date(end) - to_date(start)).days


# ── storage ──────────────────────────────────────────────────────────────────

def upsert_points(session: Session, rows: Iterable[dict], source: str = "") -> int:
    """Insert or overwrite points keyed by (project_id, metric, dim, day)."""
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    stamp = now_ts()
    prepared = [{
        "project_id": int(r.get("project_id") or 0),
        "metric": r["metric"],
        "dim": r.get("dim") or "",
        "day": str(r["day"])[:10],
        "value": float(r.get("value") or 0),
        "span_days": max(1, int(r.get("span_days") or 1)),
        "source": r.get("source") or source,
        "updated_at": stamp,
    } for r in rows]
    table = MetricPoint.__table__
    for i in range(0, len(prepared), 100):  # stay under SQLite's bound-variable limit
        stmt = sqlite_insert(table).values(prepared[i:i + 100])
        stmt = stmt.on_conflict_do_update(
            index_elements=["project_id", "metric", "dim", "day"],
            set_={c: stmt.excluded[c] for c in ("value", "span_days", "source", "updated_at")},
        )
        session.exec(stmt)
    session.commit()
    return len(prepared)


def _points(session: Session, project_id: int, metric: str, dim: str,
            start: Optional[str] = None, end: Optional[str] = None) -> list[MetricPoint]:
    q = select(MetricPoint).where(MetricPoint.project_id == int(project_id or 0),
                                  MetricPoint.metric == metric, MetricPoint.dim == (dim or ""))
    if start:
        q = q.where(MetricPoint.day >= start)
    if end:
        q = q.where(MetricPoint.day <= end)
    return list(session.exec(q.order_by(MetricPoint.day)).all())


def daily_series(session: Session, project_id: int, metric: str, start: str, end: str,
                 dim: str = "") -> list[tuple[str, Optional[float]]]:
    """(day, value) for every day in [start, end]; None where nothing was recorded.

    Flow points spanning several days are spread evenly over them, so a weekly
    manual entry shows up as a daily average instead of one spike.
    """
    days = day_range(start, end)
    out: dict[str, Optional[float]] = {d: None for d in days}
    if metric_kind(metric) == "flow":
        # a span point dated after `end` can't reach back into it, one dated before
        # `start` could only if its span crosses start — widen the query by 60 days
        for p in _points(session, project_id, metric, dim, shift(start, -60) if days else None, end):
            span = max(1, p.span_days or 1)
            share = (p.value or 0) / span
            for d in day_range(shift(p.day, -(span - 1)), p.day):
                if d in out:
                    out[d] = (out[d] or 0) + share
    else:
        for p in _points(session, project_id, metric, dim, start, end):
            out[p.day] = p.value
    return [(d, out[d]) for d in days]


def window_sum(session: Session, project_id: int, metric: str, start: str, end: str,
               dim: str = "") -> tuple[float, int]:
    """Sum of a flow metric over [start, end] and how many of those days had data."""
    vals = [v for _, v in daily_series(session, project_id, metric, start, end, dim) if v is not None]
    return float(sum(vals)), len(vals)


def level_on_or_before(session: Session, project_id: int, metric: str, day: str,
                       dim: str = "") -> Optional[MetricPoint]:
    q = (select(MetricPoint)
         .where(MetricPoint.project_id == int(project_id or 0), MetricPoint.metric == metric,
                MetricPoint.dim == (dim or ""), MetricPoint.day <= day)
         .order_by(MetricPoint.day.desc()).limit(1))
    return session.exec(q).first()


def level_on_or_after(session: Session, project_id: int, metric: str, day: str,
                      dim: str = "") -> Optional[MetricPoint]:
    q = (select(MetricPoint)
         .where(MetricPoint.project_id == int(project_id or 0), MetricPoint.metric == metric,
                MetricPoint.dim == (dim or ""), MetricPoint.day >= day)
         .order_by(MetricPoint.day).limit(1))
    return session.exec(q).first()


def last_day(session: Session, project_ids: list[int], metric: str, dim: str = "") -> Optional[str]:
    """Latest day with a point of this metric for any of the projects."""
    if not project_ids:
        return None
    q = (select(MetricPoint.day)
         .where(MetricPoint.project_id.in_([int(p or 0) for p in project_ids]),
                MetricPoint.metric == metric, MetricPoint.dim == (dim or ""))
         .order_by(MetricPoint.day.desc()).limit(1))
    return session.exec(q).first()


def dim_totals(session: Session, project_id: int, metric: str, prefix: str, start: str, end: str,
               limit: int = 5) -> list[tuple[str, float]]:
    """Biggest breakdowns ("src:Tag Page", "ref:google"…) of one-day points over [start, end],
    with the prefix stripped."""
    from sqlalchemy import func
    total = func.sum(MetricPoint.value)
    q = (select(MetricPoint.dim, total)
         .where(MetricPoint.project_id == int(project_id or 0), MetricPoint.metric == metric,
                MetricPoint.dim.like(prefix + "%"), MetricPoint.day >= start, MetricPoint.day <= end)
         .group_by(MetricPoint.dim).order_by(total.desc()).limit(limit))
    return [(d[len(prefix):], float(v or 0)) for d, v in session.exec(q).all() if v]


def has_metric(session: Session, project_id: int, metric: str) -> bool:
    q = select(MetricPoint.id).where(MetricPoint.project_id == int(project_id or 0),
                                     MetricPoint.metric == metric).limit(1)
    return session.exec(q).first() is not None


# ── follower reads with a timestamp (publication reports) ────────────────────

def add_samples(session: Session, rows: Iterable[dict], ts: datetime) -> None:
    """Store one read per {project_id, metric, value} row, all taken at `ts` (UTC)."""
    for r in rows:
        session.add(AudienceSample(project_id=int(r.get("project_id") or 0), metric=r["metric"],
                                   ts=ts, value=float(r["value"])))
    session.commit()


def latest_sample_ts(session: Session) -> Optional[datetime]:
    q = select(AudienceSample.ts).order_by(AudienceSample.ts.desc()).limit(1)
    return session.exec(q).first()


class Samples:
    """Follower reads since `start`, loaded once and looked up by time."""

    def __init__(self, session: Session, start: datetime) -> None:
        self._series: dict[tuple[int, str], tuple[list[datetime], list[float]]] = {}
        q = select(AudienceSample).where(AudienceSample.ts >= start).order_by(AudienceSample.ts)
        for s in session.exec(q).all():
            times, values = self._series.setdefault((s.project_id, s.metric), ([], []))
            times.append(s.ts)
            values.append(s.value)

    def at(self, metric: str, ts: datetime, project_id: int = 0,
           not_before: Optional[datetime] = None) -> Optional[tuple[datetime, float]]:
        """The last read at or before `ts` (and not older than `not_before`)."""
        from bisect import bisect_right
        times, values = self._series.get((int(project_id or 0), metric), ([], []))
        i = bisect_right(times, ts) - 1
        if i < 0 or (not_before is not None and times[i] < not_before):
            return None
        return times[i], values[i]

    def first(self, metric: str, project_id: int = 0,
              since: Optional[datetime] = None) -> Optional[tuple[datetime, float]]:
        """The first read at or after `since`."""
        from bisect import bisect_left
        times, values = self._series.get((int(project_id or 0), metric), ([], []))
        i = bisect_left(times, since) if since is not None else 0
        return (times[i], values[i]) if i < len(times) else None

    def latest(self, metric: str, project_id: int = 0) -> Optional[tuple[datetime, float]]:
        times, values = self._series.get((int(project_id or 0), metric), ([], []))
        return (times[-1], values[-1]) if times else None


# ── key/value state ──────────────────────────────────────────────────────────

def kv_get(session: Session, key: str, default=None):
    row = session.get(KVState, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except Exception:
        return default


def kv_set(session: Session, key: str, value) -> None:
    row = session.get(KVState, key) or KVState(key=key)
    row.value = json.dumps(value, ensure_ascii=False)
    row.updated_at = now_ts()
    session.add(row)
    session.commit()


# ── goals ────────────────────────────────────────────────────────────────────

@dataclass
class GoalProgress:
    current: float = 0.0            # amount achieved toward the target
    target: float = 0.0             # amount needed (for "total" goals: target − baseline)
    pct: float = 0.0                # current / target, may exceed 1
    baseline: Optional[float] = None
    level_now: Optional[float] = None
    rate_now: Optional[float] = None     # per day over the last 7 days of the goal
    rate_before: Optional[float] = None  # per day over the 14 days before the start
    eta_day: Optional[str] = None
    days_elapsed: int = 0
    days_left: Optional[int] = None
    has_data: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def remaining(self) -> float:
        return max(0.0, self.target - self.current)

    @property
    def reached(self) -> bool:
        return self.has_data and self.target > 0 and self.current >= self.target


def goal_baseline(session: Session, goal: Goal) -> Optional[float]:
    """Value of a level metric when the goal started (first value after it if none before)."""
    if goal.baseline is not None:
        return goal.baseline
    p = (level_on_or_before(session, goal.project_id, goal.metric, goal.start_day, goal.dim)
         or level_on_or_after(session, goal.project_id, goal.metric, goal.start_day, goal.dim))
    return p.value if p else None


def goal_progress(session: Session, goal: Goal, today: Optional[str] = None) -> GoalProgress:
    today = today or today_str()
    gp = GoalProgress()
    gp.days_elapsed = max(0, days_between(goal.start_day, today)) + 1
    if goal.deadline:
        gp.days_left = days_between(today, goal.deadline)
    recent_start = max(goal.start_day, shift(today, -6))

    if metric_kind(goal.metric) == "flow":
        gp.target = float(goal.target or 0)
        total, n = window_sum(session, goal.project_id, goal.metric, goal.start_day, today, goal.dim)
        gp.current, gp.has_data = total, n > 0
        rec, rec_n = window_sum(session, goal.project_id, goal.metric, recent_start, today, goal.dim)
        if rec_n:
            gp.rate_now = rec / rec_n
        before, before_n = window_sum(session, goal.project_id, goal.metric,
                                      shift(goal.start_day, -14), shift(goal.start_day, -1), goal.dim)
        if before_n:
            gp.rate_before = before / before_n
    else:
        base = goal_baseline(session, goal)
        last = level_on_or_before(session, goal.project_id, goal.metric, today, goal.dim)
        gp.baseline = base
        gp.level_now = last.value if last else None
        gp.has_data = base is not None and last is not None
        if goal.mode == "total":
            gp.target = float(goal.target or 0) - (base or 0)
        else:
            gp.target = float(goal.target or 0)
        if gp.has_data:
            gp.current = (last.value or 0) - (base or 0)
            week_ago = level_on_or_before(session, goal.project_id, goal.metric, shift(today, -7), goal.dim)
            ref = week_ago if (week_ago and week_ago.day >= goal.start_day) else None
            ref = ref or level_on_or_after(session, goal.project_id, goal.metric, goal.start_day, goal.dim)
            if ref and ref.day < last.day:
                gp.rate_now = ((last.value or 0) - (ref.value or 0)) / max(1, days_between(ref.day, last.day))

    if gp.target > 0:
        gp.pct = gp.current / gp.target
    if gp.has_data and not gp.reached and gp.rate_now and gp.rate_now > 0:
        gp.eta_day = shift(today, math.ceil(gp.remaining / gp.rate_now))
    return gp


def crossing_day(session: Session, goal: Goal, today: Optional[str] = None) -> Optional[str]:
    """First day the goal's progress reached its target."""
    today = today or today_str()
    if metric_kind(goal.metric) == "flow":
        acc = 0.0
        for d, v in daily_series(session, goal.project_id, goal.metric, goal.start_day, today, goal.dim):
            acc += v or 0
            if goal.target and acc >= goal.target:
                return d
        return None
    base = goal_baseline(session, goal)
    if base is None:
        return None
    need = goal.target if goal.mode == "total" else base + goal.target
    for p in _points(session, goal.project_id, goal.metric, goal.dim, goal.start_day, today):
        if p.value >= need:
            return p.day
    return None


def evaluate_goals(session: Session, today: Optional[str] = None) -> list[Goal]:
    """Mark active goals whose target is reached; returns the newly achieved ones."""
    today = today or today_str()
    done = []
    for g in session.exec(select(Goal).where(Goal.status == "active")).all():
        if g.start_day > today:
            continue
        if goal_progress(session, g, today).reached:
            g.status = "achieved"
            g.achieved_day = crossing_day(session, g, today) or today
            g.updated_at = now_ts()
            session.add(g)
            done.append(g)
    if done:
        session.commit()
    return done


# ── publications ↔ projects/goals ────────────────────────────────────────────

def parse_id(value) -> Optional[int]:
    try:
        v = int(str(value).strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def tag_brief(session: Session, brief: Brief, project_id=None, goal_id=None) -> None:
    """Remember which project a new publication belongs to and which goal it serves."""
    pid, gid = parse_id(project_id), parse_id(goal_id)
    goal = session.get(Goal, gid) if gid else None
    if pid is None and goal is not None and goal.project_id:
        pid = goal.project_id
    if pid is not None and session.get(Project, pid) is not None:
        brief.project_id = pid
        session.add(brief)
    if goal is not None:
        exists = session.exec(select(GoalLink).where(GoalLink.goal_id == goal.id,
                                                     GoalLink.brief_id == brief.id)).first()
        if exists is None:
            session.add(GoalLink(goal_id=goal.id, brief_id=brief.id))
    session.commit()


def _brief_title(b: Brief) -> str:
    text = (b.title or b.text_en or b.text_ru or "").strip().splitlines()
    t = text[0] if text else f"Post #{b.id}"
    return t if len(t) <= 90 else t[:89] + "…"


def goal_events(session: Session, goal: Goal, start: str, end: str) -> list[dict]:
    """Posts and manual actions to mark on a goal's chart, oldest first.

    A publication counts when it was explicitly made for the goal, or when it went out
    inside the window and belongs to the goal's project (any publication for a global
    goal — followers grow from all of them).
    """
    linked = set(session.exec(select(GoalLink.brief_id).where(GoalLink.goal_id == goal.id)).all())
    q = select(Post, Brief).where(Post.brief_id == Brief.id)
    rows = session.exec(q.where(Post.status.in_([PostStatusEnum.done, PostStatusEnum.awaiting_finish,
                                                 PostStatusEnum.queued, PostStatusEnum.running]))).all()
    by_brief: dict[int, dict] = {}
    for post, brief in rows:
        when = post.published_at or post.scheduled_at or post.created_at
        if when is None:
            continue
        day = when.date().isoformat()
        is_linked = brief.id in linked
        in_scope = brief.project_id == goal.project_id if goal.project_id else True
        if not (is_linked or in_scope) or not (start <= day <= end):
            continue
        ev = by_brief.setdefault(brief.id, {
            "day": day, "kind": "post", "title": _brief_title(brief), "platforms": [],
            "linked": is_linked, "brief_id": brief.id, "url": None,
        })
        ev["day"] = min(ev["day"], day)
        plat = post.platform.value if hasattr(post.platform, "value") else str(post.platform)
        if plat not in ev["platforms"]:
            ev["platforms"].append(plat)
        if post.platform_url and not ev["url"]:
            ev["url"] = post.platform_url
    events = list(by_brief.values())
    acts = select(Activity).where(Activity.day >= start, Activity.day <= end)
    for a in session.exec(acts).all():
        if a.goal_id == goal.id or (goal.project_id and a.project_id == goal.project_id and a.goal_id is None):
            events.append({"day": a.day, "kind": "activity", "title": a.title, "platforms": [],
                           "linked": a.goal_id == goal.id, "activity_id": a.id, "url": None})
    events.sort(key=lambda e: (e["day"], e["kind"]))
    return events


# ── formatting (used by templates) ───────────────────────────────────────────

def fmt_num(value, unit: str = "") -> str:
    """1284 → '1,284', 12900 → '12.9K'; floats keep one decimal only when needed."""
    if value is None:
        return "—"
    v = float(value)
    sign = "−" if v < 0 else ""
    a = abs(v)
    if a >= 100_000:
        s = f"{a / 1000:.0f}K"
    elif a >= 10_000:
        s = f"{a / 1000:.1f}K".replace(".0K", "K")
    elif a == int(a):
        s = f"{int(a):,}"
    else:
        s = f"{a:,.1f}"
    return f"{sign}{unit}{s}" if unit == "$" else f"{sign}{s}{unit}"


def fmt_delta(value) -> str:
    if value is None:
        return ""
    if value == 0:
        return "±0"
    return ("+" if value > 0 else "") + fmt_num(value)


def fmt_day(day: Optional[str]) -> str:
    if not day:
        return "—"
    d = to_date(day)
    s = f"{d:%b} {d.day}"
    return s if d.year == date.today().year else f"{s}, {d.year}"


def as_datetime(day: str) -> datetime:
    return datetime.combine(to_date(day), datetime.min.time())
