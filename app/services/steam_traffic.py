"""Steam store-page traffic (visits, impressions) from Steamworks' Traffic Breakdown.

Steam has no API for it, but the report page serves a CSV
(/apps/navtrafficstats/<appid>?format=csv&preset_date_range=custom&start_date=MM/DD/YYYY&end_date=…),
one range per request. The "IndiePulse ← Steam" bookmarklet (steam_bookmarklet.js)
downloads one CSV per game per day inside the user's logged-in browser and hands
them to /steam/traffic/import in the URL fragment; this module parses and stores them.

CSV rows are "Page / Category","Page / Feature",Impressions,Visits[,Owner Impressions,Owner Visits].
Rows with category "Country" repeat the same traffic by country. The "Bot Traffic"
category is counted in Steamworks' VISITS total; IndiePulse keeps it apart.
"""
from __future__ import annotations

import base64
import csv
import gzip
import hmac
import io
import json
import secrets
from datetime import datetime
from typing import Optional
from urllib.parse import quote

from sqlmodel import Session, select

from app.models.models import MetricPoint, Project
from app.services import marketing as mk

BOT_CATEGORY = "Bot Traffic"
FIRST_SYNC_DAYS = 90           # history asked for on the first sync
MAX_PAYLOAD_BYTES = 30_000_000
BOOKMARKLET_DEFAULT_DAYS = 35  # when the bookmarklet is clicked on a page IndiePulse didn't open
TRAFFIC_METRICS = ("steam_visits", "steam_impressions")


def traffic_url(appid) -> str:
    return f"https://partner.steamgames.com/apps/navtrafficstats/{int(appid)}"


# ── parsing ──────────────────────────────────────────────────────────────────

def _int(value) -> int:
    try:
        return int(float(str(value).replace(",", "").strip() or 0))
    except ValueError:
        return 0


def parse_navtraffic_csv(text: str) -> dict:
    """Totals of one Traffic Breakdown CSV: impressions, visits without bots,
    bot visits, and visits per source category / external site / country."""
    reader = csv.reader(io.StringIO((text or "").lstrip("﻿")))
    header = [h.strip() for h in next(reader, [])]
    if not header or header[0] != "Page / Category" or "Visits" not in header or "Impressions" not in header:
        raise ValueError("doesn’t look like a Steam traffic CSV")
    iv, vv = header.index("Impressions"), header.index("Visits")
    out = {"impressions": 0, "visits": 0, "bot_visits": 0, "sources": {}, "referrers": {}, "countries": {}}

    def add(bucket: str, key: str, n: int) -> None:
        if key and n:
            out[bucket][key] = out[bucket].get(key, 0) + n

    for row in reader:
        if len(row) <= max(iv, vv):
            continue
        category, feature = row[0].strip(), row[1].strip()
        impressions, visits = _int(row[iv]), _int(row[vv])
        if category == "Country":
            add("countries", feature.upper(), visits)
            continue
        out["impressions"] += impressions
        if category == BOT_CATEGORY:
            out["bot_visits"] += visits
            continue
        out["visits"] += visits
        add("sources", category, visits)
        if category == "External Website":
            add("referrers", feature.lower(), visits)
    return out


def traffic_rows(project_id: int, day: str, parsed: dict) -> list[dict]:
    """Metric points for one game-day. steam_visits "" excludes bots; dims:
    bot, src:<category>, ref:<site>, country:<CC> (as Steam reports it)."""
    rows = [
        {"metric": "steam_visits", "dim": "", "value": parsed["visits"]},
        {"metric": "steam_visits", "dim": "bot", "value": parsed["bot_visits"]},
        {"metric": "steam_impressions", "dim": "", "value": parsed["impressions"]},
    ]
    for prefix, bucket in (("src", "sources"), ("ref", "referrers"), ("country", "countries")):
        for key, n in parsed[bucket].items():
            rows.append({"metric": "steam_visits", "dim": f"{prefix}:{key}"[:120], "value": n})
    for r in rows:
        r.update(project_id=project_id, day=day, source="steam_traffic")
    return rows


def decode_payload(blob: str) -> dict:
    """The bookmarklet's URL fragment: base64url of (gzipped) JSON."""
    blob = (blob or "").strip().lstrip("#")
    try:
        raw = base64.urlsafe_b64decode(blob + "=" * (-len(blob) % 4))
        if raw[:2] == b"\x1f\x8b":
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:
                raw = f.read(MAX_PAYLOAD_BYTES + 1)
        if len(raw) > MAX_PAYLOAD_BYTES:
            raise ValueError("too much data")
        data = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"data is corrupted ({exc.__class__.__name__})")
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ValueError("data is corrupted")
    return data


# ── bookmarklet token ────────────────────────────────────────────────────────

def sync_token() -> str:
    """Secret baked into the bookmarklet, so another website can't feed fake numbers
    through the import page. Created on first use."""
    from app.services.settings import load_settings, save_settings
    tok = (load_settings().get("steam_traffic_token") or "").strip()
    if not tok:
        tok = secrets.token_urlsafe(18)
        save_settings({"steam_traffic_token": tok})
    return tok


def token_ok(token) -> bool:
    from app.services.settings import load_settings
    expected = (load_settings().get("steam_traffic_token") or "").strip()
    return bool(expected) and hmac.compare_digest(str(token or ""), expected)


# ── storing ──────────────────────────────────────────────────────────────────

def _clear_days(session: Session, project_id: int, days: set[str]) -> None:
    """Drop what an import replaces: earlier points of these days (every dim), and
    manual multi-day entries that overlap them — they'd be counted twice."""
    lo, hi = min(days), mk.shift(max(days), 60)  # a span point is dated at its last day
    q = select(MetricPoint).where(MetricPoint.project_id == project_id,
                                  MetricPoint.metric.in_(TRAFFIC_METRICS),
                                  MetricPoint.day >= lo, MetricPoint.day <= hi)
    for p in session.exec(q).all():
        span = max(1, p.span_days or 1)
        covered = {p.day} if span == 1 else set(mk.day_range(mk.shift(p.day, -(span - 1)), p.day))
        if covered & days:
            session.delete(p)
    session.commit()


def import_payload(session: Session, data: dict) -> dict:
    """Store the bookmarklet's CSVs. Returns a per-project summary for the result page."""
    from app.services.marketing_collect import record_status, steam_projects
    by_app = {int(p.steam_app_id): p for p in steam_projects(session)}
    parsed_by_pid: dict[int, dict[str, dict]] = {}
    unknown, bad = set(), 0
    for item in data.get("rows") or []:
        try:
            appid = int(item.get("appid") or 0)
            day = mk.to_date(item.get("day")).isoformat()
            parsed = parse_navtraffic_csv(item.get("csv") or "")
        except (ValueError, TypeError, AttributeError):
            bad += 1
            continue
        project = by_app.get(appid)
        if project is None:
            unknown.add(appid)
            continue
        parsed_by_pid.setdefault(project.id, {})[day] = parsed

    projects = []
    for pid, days in parsed_by_pid.items():
        _clear_days(session, pid, set(days))
        rows = [r for day, parsed in days.items() for r in traffic_rows(pid, day, parsed)]
        mk.upsert_points(session, rows)
        ordered = sorted(days)
        last7 = [days[d] for d in ordered[-7:]]
        visits, bots = sum(x["visits"] for x in last7), sum(x["bot_visits"] for x in last7)
        project = session.get(Project, pid)
        projects.append({
            "project": project.name if project else str(pid), "days": len(ordered),
            "first": ordered[0], "last": ordered[-1],
            "visits7": visits, "impressions7": sum(x["impressions"] for x in last7),
            "bot_share7": bots / (visits + bots) if (visits + bots) else 0.0,
        })
    stamp = datetime.now().isoformat(timespec="seconds")
    mk.kv_set(session, "steam_traffic_sync", {"at": stamp, "days": data.get("days"),
                                              "failed": (data.get("failed") or [])[:20]})
    if projects:
        info = "; ".join(f"{p['project']}: {p['days']} d through {mk.fmt_day(p['last'])}" for p in projects)
        record_status(session, "steam_traffic", True, info=info)
        mk.evaluate_goals(session)
    return {"projects": projects, "unknown_apps": sorted(unknown), "bad": bad,
            "failed": (data.get("failed") or [])[:20]}


# ── what to ask Steam for ────────────────────────────────────────────────────

def last_traffic_day(session: Session, project_id: int) -> Optional[str]:
    q = (select(MetricPoint.day).where(MetricPoint.project_id == project_id,
                                       MetricPoint.metric == "steam_visits",
                                       MetricPoint.dim == "", MetricPoint.source == "steam_traffic")
         .order_by(MetricPoint.day.desc()).limit(1))
    return session.exec(q).first()


def sync_plan(session: Session, today: Optional[str] = None) -> dict:
    """Games to sync and how many days back: from the oldest last-synced day (a game
    never synced asks for FIRST_SYNC_DAYS). Steam has no data for today."""
    from app.services.marketing_collect import steam_projects
    today = today or datetime.utcnow().date().isoformat()  # Steam days are GMT days
    projects = steam_projects(session)
    lasts = {p.id: last_traffic_day(session, p.id) for p in projects}
    if not projects:
        days = 0
    elif any(v is None for v in lasts.values()):
        days = FIRST_SYNC_DAYS
    else:
        days = max(3, mk.days_between(min(lasts.values()), today) + 1)  # re-read a couple of days
    return {"projects": projects, "last": lasts, "days": min(days, 400),
            "apps": [int(p.steam_app_id) for p in projects]}


def steamworks_sync_url(session: Session) -> Optional[str]:
    """The Traffic page of the first game, with a hint the bookmarklet reads."""
    plan = sync_plan(session)
    if not plan["apps"]:
        return None
    apps = ",".join(str(a) for a in plan["apps"])
    return f"{traffic_url(plan['apps'][0])}#indiepulse:days={plan['days']}:apps={apps}"


def bookmarklet(origin: str, token: str, apps: list[int], days: int = BOOKMARKLET_DEFAULT_DAYS) -> str:
    """javascript: URL of the bookmarklet, with this IndiePulse's address and token."""
    import os
    path = os.path.join(os.path.dirname(__file__), "steam_bookmarklet.js")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    src = src[src.index("(async"):]  # drop the header comment
    src = (src.replace("__ORIGIN__", json.dumps(origin.rstrip("/")))
              .replace("__TOKEN__", json.dumps(token))
              .replace("__APPS__", json.dumps([int(a) for a in apps]))
              .replace("__DAYS__", str(int(days))))
    lines = [ln.strip() for ln in src.splitlines()]
    return "javascript:" + quote("\n".join(ln for ln in lines if ln), safe="")
