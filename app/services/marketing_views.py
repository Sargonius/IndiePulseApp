"""View models for the marketing home screen and the goal pages."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

from sqlmodel import Session, select

from app.models.models import Brief, Goal, GoalLink, Post, PostStatusEnum, Project, UtmLink
from app.services import audience
from app.services import marketing as mk
from app.services import publications as pubs
from app.services import steam_links, steam_traffic, utm
from app.services.marketing_collect import source_statuses, steam_projects, wishlist_backfill_progress
from app.services.settings import load_settings

TRAFFIC_REMIND_DAYS = 7  # remind to run the Steam traffic bookmarklet after a week
HOME_PUBLICATIONS_DAYS = 14
HOME_PUBLICATIONS_MAX = 8


def _filled(series: list[tuple[str, Optional[float]]], carry: bool) -> list[float]:
    """Numbers for a sparkline: gaps become 0 (flows) or the previous value (levels)."""
    vals = [v for _, v in series]
    if not carry:
        return [float(v or 0) for v in vals]
    last = next((v for v in vals if v is not None), 0.0)
    out = []
    for v in vals:
        if v is not None:
            last = v
        out.append(float(last))
    return out


def _tile(label: str, value, delta=None, sub: str = "", spark=None, spark_days=None, unit: str = "",
          href: str = "", kind: str = "flow") -> dict:
    return {
        "label": label, "value": mk.fmt_num(value, unit), "raw": value,
        "delta": mk.fmt_delta(delta) if delta is not None else "",
        "delta_dir": ("up" if (delta or 0) > 0 else "down" if (delta or 0) < 0 else "flat"),
        "sub": sub, "spark": spark, "spark_days": spark_days, "href": href, "kind": kind,
    }


def _flow_tile(session: Session, pids: list[int], metric: str, label: str, today: str, unit: str = "",
               href: str = "", end: Optional[str] = None) -> Optional[dict]:
    """7-day sum vs the week before. `end` = last day with data, for metrics that
    arrive in batches (Steam traffic), so a week not synced yet doesn't read as a drop."""
    if not any(mk.has_metric(session, pid, metric) for pid in pids):
        return None
    end = min(end or today, today)
    start30 = mk.shift(end, -29)
    days = mk.day_range(start30, end)
    per_day = [0.0] * len(days)
    this_w = prev_w = 0.0
    for pid in pids:
        series = mk.daily_series(session, pid, metric, start30, end)
        for i, (_, v) in enumerate(series):
            per_day[i] += v or 0
        this_w += mk.window_sum(session, pid, metric, mk.shift(end, -6), end)[0]
        prev_w += mk.window_sum(session, pid, metric, mk.shift(end, -13), mk.shift(end, -7))[0]
    sub = "last 7 days" if end == today else f"7 days to {mk.fmt_day(end)}"
    return _tile(label, this_w, this_w - prev_w, sub, per_day, days, unit, href)


def _level_tile(session: Session, pid: int, metric: str, label: str, today: str, href: str = "") -> Optional[dict]:
    last = mk.level_on_or_before(session, pid, metric, today)
    if last is None:
        return None
    week = mk.level_on_or_before(session, pid, metric, mk.shift(today, -7))
    start30 = mk.shift(today, -29)
    series = mk.daily_series(session, pid, metric, start30, today)
    delta = (last.value - week.value) if week else None
    return _tile(label, last.value, delta, "last 7 days" if week else f"as of {mk.fmt_day(last.day)}",
                 _filled(series, carry=True), [d for d, _ in series], href=href, kind="level")


def _change(session: Session, metric: str, last, today: str, days: int) -> dict:
    """Change of a level metric over `days`; a younger series counts from its first read."""
    ref = mk.level_on_or_before(session, 0, metric, mk.shift(today, -days))
    since = None
    if ref is None:
        ref = mk.level_on_or_after(session, 0, metric, mk.shift(today, -days))
        since = ref.day if ref else None
    if ref is None or (since and ref.day >= last.day):
        return {"value": None, "since": None}
    return {"value": last.value - ref.value, "since": since}


def audience_rows(session: Session, cfg: dict, statuses: dict[str, dict], today: str) -> tuple[list[dict], list[str]]:
    """Followers per social account (biggest first) and the accounts not connected."""
    rows, missing = [], []
    start30 = mk.shift(today, -29)
    for ch in audience.channels(cfg):
        st = statuses.get(ch.key) or {}
        last = mk.level_on_or_before(session, 0, ch.metric, today)
        if last is None:
            if not ch.ready(cfg):
                missing.append(ch.label)
            elif not st.get("ok", True):
                rows.append({"label": ch.label, "value": None, "error": st.get("error"), "spark": None})
            continue
        series = mk.daily_series(session, 0, ch.metric, start30, today)
        rows.append({"label": ch.label, "metric": ch.metric, "value": last.value,
                     "d7": _change(session, ch.metric, last, today, 7),
                     "d30": _change(session, ch.metric, last, today, 30),
                     "spark": _filled(series, carry=True), "days": [d for d, _ in series],
                     "error": None if st.get("ok", True) else st.get("error"), "info": st.get("info")})
    rows.sort(key=lambda r: -(r["value"] or 0))
    return rows, missing


def _goal_row(session: Session, g: Goal, today: str, projects: dict[int, Project]) -> dict:
    gp = mk.goal_progress(session, g, today)
    p = projects.get(g.project_id)
    return {"goal": g, "gp": gp, "project": p, "metric_label": mk.metric_label(g.metric),
            "pct100": max(0, min(100, round(gp.pct * 100))) if gp.target else 0}


def home_context(session: Session, today: Optional[str] = None) -> dict:
    today = today or mk.today_str()
    now = datetime.now()
    cfg = load_settings()
    projects = list(session.exec(select(Project).order_by(Project.sort_order, Project.id)).all())
    by_id = {p.id: p for p in projects}
    active = [p for p in projects if (p.status or "active") == "active"]
    paused = [p for p in projects if p.status == "paused"]
    steam_ids = [p.id for p in active if p.steam_app_id]

    kpis = [t for t in (
        _flow_tile(session, steam_ids, "steam_wishlist_adds", "Wishlist adds", today),
        _flow_tile(session, steam_ids, "steam_visits", "Steam page visits", today, href="/steam/traffic",
                   end=mk.last_day(session, steam_ids, "steam_visits")),
        _flow_tile(session, steam_ids, "steam_units", "Steam sales", today),
        _level_tile(session, 0, "followers_total", "Social followers", today, href="#audience"),
        _flow_tile(session, [0], "post_views", "Post views", today, href="/publications"),
        _flow_tile(session, [0], "post_reactions", "Post reactions", today, href="/publications"),
    ) if t]

    goals = session.exec(select(Goal).where(Goal.status == "active").order_by(Goal.id)).all()
    goal_rows = [_goal_row(session, g, today, by_id) for g in goals]

    # posts per project: planned (queued, in the future) and published in the last 7 days
    post_rows = session.exec(select(Post, Brief).where(Post.brief_id == Brief.id,
                                                       Brief.project_id.is_not(None))).all()
    planned: dict[int, list[datetime]] = {}
    published: dict[int, int] = {}
    week_ago_utc = datetime.utcnow() - timedelta(days=7)  # published_at is UTC, scheduled_at local
    for post, brief in post_rows:
        if post.status == PostStatusEnum.queued and post.scheduled_at and post.scheduled_at >= now:
            planned.setdefault(brief.project_id, []).append(post.scheduled_at)
        if post.status == PostStatusEnum.done and post.published_at and post.published_at >= week_ago_utc:
            published[brief.project_id] = published.get(brief.project_id, 0) + 1

    cards = []
    for p in active:
        tiles = [t for t in (
            _flow_tile(session, [p.id], "steam_wishlist_adds", "Wishlist adds", today,
                       href=f"/goals?project_id={p.id}"),
            _level_tile(session, p.id, "steam_wishlist_balance", "Total wishlists", today),
            _flow_tile(session, [p.id], "steam_units", "Sales", today),
            _flow_tile(session, [p.id], "steam_visits", "Page visits", today, href="/steam/traffic",
                       end=mk.last_day(session, [p.id], "steam_visits")),
            _flow_tile(session, [p.id], "steam_impressions", "Steam impressions", today,
                       end=mk.last_day(session, [p.id], "steam_impressions")),
            _level_tile(session, p.id, "steam_followers", "Steam followers", today),
        ) if t]
        plans = sorted(planned.get(p.id, []))
        cards.append({
            "p": p, "stage": mk.PROJECT_STAGES.get(p.stage or ""),
            "steam_url": f"https://store.steampowered.com/app/{p.steam_app_id}/" if p.steam_app_id else None,
            "links": steam_links.app_links(p.steam_app_id) if p.steam_app_id else [],
            "tiles": tiles,
            "goals": [r for r in goal_rows if r["goal"].project_id == p.id],
            "planned": len(plans), "next_post": plans[0] if plans else None,
            "published7": published.get(p.id, 0),
            "views7": mk.window_sum(session, p.id, "post_views", mk.shift(today, -6), today)[0],
            "reactions7": mk.window_sum(session, p.id, "post_reactions", mk.shift(today, -6), today)[0],
        })

    inbox = []
    drafts = session.exec(select(Post).where(Post.status == PostStatusEnum.awaiting_finish,
                                             Post.created_at >= datetime.utcnow() - timedelta(days=14))).all()
    if drafts:
        names = {"tiktok": "TikTok", "ig_reels": "Instagram"}
        counts: dict[str, int] = {}
        for d in drafts:
            plat = d.platform.value if hasattr(d.platform, "value") else str(d.platform)
            counts[names.get(plat, plat)] = counts.get(names.get(plat, plat), 0) + 1
        inbox.append({"icon": "📲", "text": "Finish in the app: " + ", ".join(f"{k} {v}" for k, v in counts.items()),
                      "href": "/shorts/board"})
    failed = session.exec(select(Post).where(Post.status == PostStatusEnum.error,
                                             Post.updated_at >= datetime.utcnow() - timedelta(days=7))).all()
    if failed:
        inbox.append({"icon": "⚠️", "text": f"Publish errors this week: {len(failed)}", "href": "/posts"})
    stale = [(p, steam_traffic.last_traffic_day(session, p.id)) for p in active if p.steam_app_id]
    stale = [(p, d) for p, d in stale if d is None or mk.days_between(d, today) > TRAFFIC_REMIND_DAYS]
    if stale:
        if mk.kv_get(session, "steam_traffic_sync") is None:
            inbox.append({"icon": "📈", "href": "/steam/traffic",
                          "text": "Connect Steam traffic: daily visits and impressions — add the bookmarklet to your browser once"})
        else:
            known = [d for _, d in stale if d]
            inbox.append({"icon": "📈", "kind": "traffic", "url": steam_traffic.steamworks_sync_url(session),
                          "text": "Steam traffic not updated" + (f" since {mk.fmt_day(min(known))}" if known else "")})
    loading = wishlist_backfill_progress(session)
    if loading and (cfg.get("steam_financial_api_key") or "").strip():
        inbox.append({"icon": "⏳", "text": "Loading wishlist history from Steam: "
                      + ", ".join(f"{x['project'].name} {round(x['done'] * 100)}%" for x in loading)
                      + " — the “Total wishlists” tile appears at 100%"})
    if steam_ids and not (cfg.get("steam_financial_api_key") or "").strip():
        inbox.append({"icon": "🔑", "text": "Add a Steam Financial API key — wishlists and sales will be collected automatically",
                      "href": "/accounts#section_steam"})
    if not any(p.steam_app_id for p in projects):
        inbox.append({"icon": "🗂", "text": "Set a Steam App ID on your projects — no Steam metrics without it",
                      "href": "/projects"})
    statuses = source_statuses(session)
    for st in statuses:
        if not st.get("ok"):
            label = f"{st['label']}: followers" if st["audience"] and st["source"] != audience.STEAM_KEY else st["label"]
            inbox.append({"icon": "⛔", "text": f"{label}: {st.get('error') or 'error'}",
                          "href": "/steam/traffic" if st["source"] == "steam_traffic" else "/accounts"})
    recent_wins = session.exec(select(Goal).where(Goal.status == "achieved",
                                                  Goal.achieved_day >= mk.shift(today, -14))).all()
    for g in recent_wins:
        inbox.append({"icon": "🎯", "text": f"Goal reached {mk.fmt_day(g.achieved_day)}: {g.title}",
                      "href": f"/goals/{g.id}"})

    aud_rows, aud_missing = audience_rows(session, cfg, {s["source"]: s for s in statuses}, today)
    recent = pubs.published(session, datetime.utcnow() - timedelta(days=HOME_PUBLICATIONS_DAYS))
    aud_statuses = [s for s in statuses if s["audience"]]
    return {
        "today": today, "kpis": kpis, "cards": cards, "paused": paused,
        "global_goals": [r for r in goal_rows if not r["goal"].project_id],
        "inbox": inbox, "statuses": [s for s in statuses if not s["audience"]],
        "audience_statuses": aud_statuses,
        "audience_ok_at": max((s.get("ok_at") or "" for s in aud_statuses), default=""),
        "audience": aud_rows, "audience_missing": aud_missing,
        "followers_total": mk.level_on_or_before(session, 0, "followers_total", today),
        "publications": pubs.with_gains(session, recent[:HOME_PUBLICATIONS_MAX]),
        "publications_more": len(recent) > HOME_PUBLICATIONS_MAX,
        "projects_by_id": by_id,
    }


def publications_context(session: Session, project_id: int = 0, days: int = 30) -> dict:
    """/publications: every publication of the period with counters and follower gains."""
    projects = list(session.exec(select(Project).order_by(Project.sort_order, Project.id)).all())
    by_id = {p.id: p for p in projects}
    project_id = project_id if project_id in by_id else 0
    rows = pubs.with_gains(session, pubs.published(session, datetime.utcnow() - timedelta(days=days),
                                                   project_id=project_id or None))
    total = pubs.empty_total()
    for r in rows:
        pubs.add_total(total, r["total"])
    gained = [r["gain"]["posted"] for r in rows if r["gain"]["posted"] is not None]
    return {"rows": rows, "projects": [p for p in projects if (p.status or "active") != "archived"],
            "projects_by_id": by_id, "project_id": project_id, "days": days, "total": total,
            "gained": sum(gained) if gained else None, "periods": [7, 30, 90, 365]}


def publication_context(session: Session, brief: Brief) -> dict:
    ctx = pubs.report(session, brief)
    linked = session.exec(select(GoalLink.goal_id).where(GoalLink.brief_id == brief.id)).all()
    ctx["goals"] = [g for g in (session.get(Goal, gid) for gid in linked) if g is not None]
    ctx["local_t0"] = pubs.local_day(ctx["t0"]) if ctx["t0"] else None
    return ctx


def goals_context(session: Session, today: Optional[str] = None) -> dict:
    today = today or mk.today_str()
    projects = list(session.exec(select(Project).order_by(Project.sort_order, Project.id)).all())
    by_id = {p.id: p for p in projects}
    rows = session.exec(select(Goal).order_by(Goal.id.desc())).all()
    return {
        "today": today,
        "projects": projects,
        "metrics": mk.METRICS,
        "active": [_goal_row(session, g, today, by_id) for g in rows if g.status == "active"],
        "achieved": [_goal_row(session, g, today, by_id) for g in rows if g.status == "achieved"],
        "archived": [_goal_row(session, g, today, by_id) for g in rows if g.status == "archived"],
    }


def goal_context(session: Session, goal: Goal, today: Optional[str] = None) -> dict:
    today = today or mk.today_str()
    projects = {p.id: p for p in session.exec(select(Project)).all()}
    project = projects.get(goal.project_id)
    spec = mk.METRICS.get(goal.metric, {"label": goal.metric, "kind": "flow"})
    gp = mk.goal_progress(session, goal, today)
    chart_start = mk.shift(goal.start_day, -14)
    end = max(today, goal.start_day)
    series = mk.daily_series(session, goal.project_id, goal.metric, chart_start, end, goal.dim)
    events = mk.goal_events(session, goal, chart_start, end)

    kind = spec["kind"]
    days = [d for d, _ in series]
    values = [v for _, v in series]
    cum, acc = [], 0.0
    for d, v in series:
        if d < goal.start_day:
            cum.append(None)
        else:
            acc += v or 0
            cum.append(acc)
    if kind == "flow":
        target_line = goal.target
    else:
        target_line = goal.target if goal.mode == "total" else ((gp.baseline or 0) + goal.target)
    chart = {
        "kind": kind, "label": spec["label"], "unit": spec.get("unit", ""),
        "days": days, "values": values, "cum": cum if kind == "flow" else None,
        "start": goal.start_day, "target": target_line, "baseline": gp.baseline,
        "before_rate": gp.rate_before, "achieved_day": goal.achieved_day,
        "events": [{"day": e["day"], "title": e["title"], "kind": e["kind"],
                    "platforms": e["platforms"], "linked": e["linked"]} for e in events],
    }
    by_day: dict[str, list[str]] = {}
    for e in events:
        by_day.setdefault(e["day"], []).append(e["title"])
    table = [{"day": d, "value": v, "cum": c, "events": by_day.get(d, [])}
             for d, v, c in zip(days, values, cum if kind == "flow" else [None] * len(days))
             if d >= goal.start_day]

    linked = set(session.exec(select(GoalLink.brief_id).where(GoalLink.goal_id == goal.id)).all())
    briefs = session.exec(select(Brief).order_by(Brief.id.desc()).limit(40)).all()
    link_options = [{"id": b.id, "title": mk._brief_title(b), "linked": b.id in linked,
                     "project": projects.get(b.project_id).name if b.project_id in projects else "",
                     "created": b.created_at} for b in briefs]
    return {
        "goal": goal, "project": project, "spec": spec, "gp": gp, "chart": chart, "table": table,
        "events": events, "link_options": link_options, "today": today,
        "pct100": max(0, min(100, round(gp.pct * 100))) if gp.target else 0,
        "manual": bool(spec.get("manual")),
        "default_start": mk.shift(today, -7), "default_end": mk.shift(today, -1),
        "steam_ready": bool(project and project.steam_app_id and steam_projects(session)),
        "steam_link": steam_links.metric_link(goal.metric, project.steam_app_id if project else None),
        "traffic_metric": goal.metric in steam_traffic.TRAFFIC_METRICS,
    }


def steam_traffic_context(session: Session, origin: str) -> dict:
    """The /steam/traffic page: bookmarklet, per-game status, how to read the numbers."""
    today = mk.today_str()
    plan = steam_traffic.sync_plan(session)
    rows = []
    for p in plan["projects"]:
        last = plan["last"].get(p.id)
        row = {"p": p, "last": last, "links": steam_links.app_links(p.steam_app_id)}
        if last:
            start = mk.shift(last, -6)
            row.update(
                week_from=start,
                visits7=mk.window_sum(session, p.id, "steam_visits", start, last)[0],
                bots7=mk.window_sum(session, p.id, "steam_visits", start, last, "bot")[0],
                impressions7=mk.window_sum(session, p.id, "steam_impressions", start, last)[0],
                sources=mk.dim_totals(session, p.id, "steam_visits", "src:", start, last, 4),
                referrers=mk.dim_totals(session, p.id, "steam_visits", "ref:", start, last, 4),
                countries=mk.dim_totals(session, p.id, "steam_visits", "country:", start, last, 5))
        rows.append(row)
    return {
        "rows": rows, "plan": plan, "today": today,
        "bookmarklet": steam_traffic.bookmarklet(origin, steam_traffic.sync_token(), plan["apps"]),
        "sync_url": steam_traffic.steamworks_sync_url(session),
        "last_sync": mk.kv_get(session, "steam_traffic_sync"),
        "publisher_links": steam_links.PUBLISHER_LINKS,
        "manual_start": mk.shift(today, -7), "manual_end": mk.shift(today, -1),
    }


def utm_link_url(link: UtmLink, project: Optional[Project]) -> Optional[str]:
    """The full URL of a saved link; None when the Steam page is gone (no app id)."""
    base = link.base_url or (utm.steam_base_url(project.steam_app_id) if project and project.steam_app_id else None)
    return utm.build_url(base, link.source, link.medium, link.campaign, link.content) if base else None


def _utm_row(link: UtmLink, project: Optional[Project]) -> dict:
    pl = utm.placement(link.source, link.medium) or {}
    return {"link": link, "url": utm_link_url(link, project), "source_label": utm.source_label(link.source),
            "medium_label": utm.medium_label(link.medium), "clickable": pl.get("clickable")}


def utm_context(session: Session, project_id: int = 0, campaign: str = "", show_set: bool = False) -> dict:
    """The /utm page: form data (projects, per-project campaign suggestions), the
    campaign set just generated and all saved links grouped project → campaign."""
    projects = [p for p in session.exec(select(Project).order_by(Project.sort_order, Project.id)).all()
                if (p.status or "active") != "archived"]
    projects.sort(key=lambda p: p.status == "paused")  # stable: active first, then paused
    by_id = {p.id: p for p in projects}
    links = session.exec(select(UtmLink).order_by(UtmLink.id.desc())).all()
    goals = session.exec(select(Goal).where(Goal.status != "archived").order_by(Goal.id.desc())).all()

    form_projects = {}
    for p in projects:
        used = [ln.campaign for ln in links if ln.project_id == p.id]
        from_goals = [utm.slug(g.title) for g in goals if g.project_id == p.id]
        form_projects[p.id] = {"name": p.name, "appid": p.steam_app_id,
                               "campaigns": list(dict.fromkeys(c for c in used + from_goals if c))}

    groups = []
    for p in projects:
        mine = [ln for ln in links if ln.project_id == p.id]
        if not mine:
            continue
        campaigns: dict[str, list[dict]] = {}
        for ln in sorted(mine, key=lambda ln: (ln.source, ln.medium, ln.content)):
            campaigns.setdefault(ln.campaign, []).append(_utm_row(ln, p))
        report = next((x for x in steam_links.app_links(p.steam_app_id) if x["key"] == "utm"),
                      None) if p.steam_app_id else None
        groups.append({"p": p, "report": report,
                       "campaigns": sorted(campaigns.items(), key=lambda kv: -max(r["link"].id for r in kv[1]))})

    set_rows = []
    campaign = utm.slug(campaign)
    if show_set and project_id in by_id and campaign:
        index = {(ln.source, ln.medium): ln for ln in links
                 if ln.project_id == project_id and ln.campaign == campaign and not ln.content}
        set_rows = [_utm_row(index[k], by_id[project_id]) for k in utm.CAMPAIGN_SET if k in index]

    return {
        "projects": projects, "form_projects": form_projects,
        "preselect": project_id if project_id in by_id else (projects[0].id if projects else 0),
        "campaign": campaign, "sources": utm.SOURCES, "set_rows": set_rows, "groups": groups,
        "set_labels": [utm.source_label(s) for s, _ in utm.CAMPAIGN_SET],
    }
