"""Collectors that fill metric_points: Steam wishlists and sales, followers, publications.

collect_all() runs every 15 minutes on the Huey worker (tasks.collect_marketing_metrics)
and from the home screen's "Refresh" button. The worker has a single thread that
also publishes, so follower counts (every social account, each game's Steam page)
are read in parallel within a time box, and Steam reports run on a background
thread of the worker process (start_steam_sync): the first wishlist history is one
API call per day since the store page opened — thousands of calls. Each source is
independent — a failure is stored in kv_state["collect:<source>"] (shown on the
home screen) and the other sources still run.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta
from typing import Callable, Optional

import httpx
from loguru import logger
from sqlmodel import Session, select

from app.models.models import MetricPoint, Project
from app.services import audience, steam_partner
from app.services.marketing import (add_samples, day_range, days_between, kv_get, kv_set,
                                    latest_sample_ts, shift, today_str, upsert_points)
from app.services.settings import load_settings

WISHLIST_REFRESH_DAYS = 3        # Steam revises the latest days a few times a day
WISHLIST_WORKERS = 4             # parallel GetAppWishlistReporting calls (~1.2 s each)
WISHLIST_CHUNK = 16              # history days fetched per step
SALES_INITIAL_DAYS = 400         # the first sales sync only looks this far back
AUDIENCE_EVERY = timedelta(hours=2)
AUDIENCE_WORKERS = 6
PRE_PUBLISH_FRESH = timedelta(minutes=30)  # a newer read already counts as "before the post"
PRE_PUBLISH_BUDGET_S = 10.0
STEAM_REFRESH_EVERY = timedelta(hours=2)
STEAM_SYNC_BUDGET_S = 20 * 60    # one background Steam run; the next one continues
CALL_PAUSE_S = 0.1


class Budget:
    """Wall-clock budget shared by the collectors of one run."""

    def __init__(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def left(self) -> bool:
        return time.monotonic() < self.deadline

    def seconds_left(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


def _status(session: Session, source: str) -> dict:
    return kv_get(session, f"collect:{source}", {}) or {}


def _due(session: Session, source: str, every: timedelta, force: bool) -> bool:
    if force:
        return True
    last = _status(session, source).get("ok_at")
    if not last:
        return True
    try:
        return datetime.now() - datetime.fromisoformat(last) >= every
    except ValueError:
        return True


def record_status(session: Session, source: str, ok: bool, info: str = "", error: str = "") -> None:
    st = _status(session, source)
    now = datetime.now().isoformat(timespec="seconds")
    st.update({"at": now, "ok": ok, "info": info, "error": error})
    if ok:
        st["ok_at"] = now
    kv_set(session, f"collect:{source}", st)


def steam_projects(session: Session) -> list[Project]:
    """Games with a Steam App ID, active ones first (their history loads first)."""
    rows = session.exec(select(Project).where(Project.steam_app_id.is_not(None))
                        .order_by(Project.sort_order, Project.id)).all()
    games = [p for p in rows if p.steam_app_id and (p.status or "active") != "archived"]
    return sorted(games, key=lambda p: (p.status or "active") != "active")


# ── Steam wishlists ──────────────────────────────────────────────────────────

def wishlist_rows(project_id: int, day: str, resp: dict) -> list[dict]:
    """Metric points for one GetAppWishlistReporting response."""
    s = resp.get("wishlist_summary") or {}
    adds, deletes = int(s.get("wishlist_adds") or 0), int(s.get("wishlist_deletes") or 0)
    rows = [
        {"metric": "steam_wishlist_adds", "value": adds},
        {"metric": "steam_wishlist_deletes", "value": deletes},
        {"metric": "steam_wishlist_purchases", "value": int(s.get("wishlist_purchases") or 0)},
        {"metric": "steam_wishlist_gifts", "value": int(s.get("wishlist_gifts") or 0)},
        {"metric": "steam_wishlist_net", "value": adds - deletes},
    ]
    for c in resp.get("country_summary") or []:
        code = (c.get("country_code") or "").upper()
        a = c.get("summary_actions") or {}
        c_adds, c_del = int(a.get("wishlist_adds") or 0), int(a.get("wishlist_deletes") or 0)
        if not code or not (c_adds or c_del):
            continue
        rows.append({"metric": "steam_wishlist_adds", "dim": f"country:{code}", "value": c_adds})
        rows.append({"metric": "steam_wishlist_net", "dim": f"country:{code}", "value": c_adds - c_del})
    for lang in resp.get("language_summary") or []:
        name = (lang.get("language_name") or str(lang.get("language") or "")).strip().lower()
        l_adds = int((lang.get("summary_actions") or {}).get("wishlist_adds") or 0)
        if name and l_adds:
            rows.append({"metric": "steam_wishlist_adds", "dim": f"lang:{name}", "value": l_adds})
    for r in rows:
        r.update(project_id=project_id, day=day, source="steam")
    return rows


def _rebuild_balance(session: Session, project_id: int, min_date: str, until: str) -> None:
    """Outstanding wishlists per day = running sum of adds − deletes − purchases − gifts.
    Only meaningful once the history is backfilled down to app_min_date."""
    parts = ("steam_wishlist_adds", "steam_wishlist_deletes", "steam_wishlist_purchases",
             "steam_wishlist_gifts")
    q = select(MetricPoint).where(MetricPoint.project_id == project_id, MetricPoint.dim == "",
                                  MetricPoint.metric.in_(parts), MetricPoint.day >= min_date,
                                  MetricPoint.day <= until)
    delta: dict[str, float] = {}
    for p in session.exec(q).all():
        sign = 1 if p.metric == "steam_wishlist_adds" else -1
        delta[p.day] = delta.get(p.day, 0.0) + sign * (p.value or 0)
    acc, rows = 0.0, []
    for d in day_range(min_date, until):
        acc += delta.get(d, 0.0)
        rows.append({"project_id": project_id, "metric": "steam_wishlist_balance", "day": d,
                     "value": acc, "source": "steam"})
    upsert_points(session, rows)


def _fetch_wishlist_days(session: Session, key: str, project: Project, st: dict, days: list[str]) -> None:
    """Fetch several GMT days in parallel and store them. st["oldest"] moves only
    after the whole batch is saved, so an interrupted run resumes without gaps."""
    appid = int(project.steam_app_id)
    with ThreadPoolExecutor(max_workers=max(1, min(WISHLIST_WORKERS, len(days)))) as pool:
        responses = list(pool.map(lambda d: steam_partner.wishlist_report(key, appid, d), days))
    rows = []
    for day, resp in zip(days, responses):
        # a day without wishlist activity comes back as just {"app_min_date": ...}
        if not resp:
            raise steam_partner.SteamPartnerError(
                f"app {appid}: empty response — the key has no access to this app’s reports")
        if resp.get("app_min_date"):
            st["min_date"] = steam_partner.dash_date(resp["app_min_date"])
        rows += wishlist_rows(project.id, day, resp)
    upsert_points(session, rows)
    if not st.get("oldest") or min(days) < st["oldest"]:
        st["oldest"] = min(days)
    if st.get("min_date") and st["oldest"] <= st["min_date"]:
        st["complete"] = True
    kv_set(session, f"steam_wl:{appid}", st)


def collect_steam_wishlists(session: Session, cfg: dict, budget: Budget, force: bool) -> str:
    """Refresh the last few days of every game first, then backfill history with
    whatever budget is left (oldest collected day → app_min_date)."""
    key = (cfg.get("steam_financial_api_key") or "").strip()
    projects = steam_projects(session)
    if not key or not projects:
        return "off"
    refresh = _due(session, "steam_wishlists", STEAM_REFRESH_EVERY, force)
    latest = datetime.utcnow().date().isoformat()  # wishlist days are GMT days
    states = {p.id: kv_get(session, f"steam_wl:{p.steam_app_id}", {}) or {} for p in projects}
    fetched: set[int] = set()
    for p in projects:
        st = states[p.id]
        if (refresh or not st.get("oldest")) and budget.left():
            _fetch_wishlist_days(session, key, p, st, [shift(latest, -i) for i in range(WISHLIST_REFRESH_DAYS)])
            fetched.add(p.id)
    for p in projects:
        st = states[p.id]
        while budget.left() and st.get("oldest") and not st.get("complete"):
            start = shift(st["oldest"], -1)
            days = [shift(start, -i) for i in range(WISHLIST_CHUNK if st.get("min_date") else 1)]
            days = [d for d in days if not st.get("min_date") or d >= st["min_date"]]
            if not days:
                st["complete"] = True
                kv_set(session, f"steam_wl:{p.steam_app_id}", st)
                break
            _fetch_wishlist_days(session, key, p, st, days)
            fetched.add(p.id)
            time.sleep(CALL_PAUSE_S)
    notes = []
    for p in projects:
        st = states[p.id]
        if st.get("complete") and st.get("min_date") and p.id in fetched:
            _rebuild_balance(session, p.id, st["min_date"], latest)
        if st.get("complete"):
            notes.append(f"{p.name}: history complete")
        elif st.get("oldest"):
            notes.append(f"{p.name}: history loaded back to {st['oldest']}"
                         + (f" (data since {st['min_date']})" if st.get("min_date") else ""))
    return "; ".join(notes) or "ok"


def wishlist_backfill_progress(session: Session) -> list[dict]:
    """Games whose wishlist history is still loading, with the share done (0..1)."""
    latest = datetime.utcnow().date().isoformat()
    out = []
    for p in steam_projects(session):
        st = kv_get(session, f"steam_wl:{p.steam_app_id}", {}) or {}
        if st.get("complete") or not st.get("oldest") or not st.get("min_date"):
            continue
        total = max(1, days_between(st["min_date"], latest))
        out.append({"project": p, "done": min(1.0, days_between(st["oldest"], latest) / total),
                    "oldest": st["oldest"], "min_date": st["min_date"]})
    return out


# ── Steam sales ──────────────────────────────────────────────────────────────

def sales_rows(projects_by_app: dict[int, int], day: str, items: list[dict]) -> list[dict]:
    """Units and net revenue per project (and per country) for one day's line items."""
    units: dict[tuple[int, str], float] = {}
    revenue: dict[int, float] = {}
    for it in items:
        appid = int(it.get("primary_appid") or it.get("appid") or 0)
        pid = projects_by_app.get(appid)
        if pid is None:
            continue
        n = int(it.get("net_units_sold") or 0)
        country = (it.get("country_code") or "").upper()
        units[(pid, "")] = units.get((pid, ""), 0) + n
        if country and n:
            units[(pid, f"country:{country}")] = units.get((pid, f"country:{country}"), 0) + n
        revenue[pid] = revenue.get(pid, 0.0) + steam_partner.to_float(it.get("net_sales_usd"))
    rows = [{"project_id": pid, "metric": "steam_units", "dim": dim, "day": day, "value": v}
            for (pid, dim), v in units.items()]
    rows += [{"project_id": pid, "metric": "steam_revenue_usd", "day": day, "value": round(v, 2)}
             for pid, v in revenue.items()]
    # a day with no sales still gets explicit zeros, so charts show 0 rather than a gap
    for pid in set(projects_by_app.values()):
        if (pid, "") not in units:
            rows.append({"project_id": pid, "metric": "steam_units", "day": day, "value": 0})
            rows.append({"project_id": pid, "metric": "steam_revenue_usd", "day": day, "value": 0})
    for r in rows:
        r["source"] = "steam"
    return rows


def collect_steam_sales(session: Session, cfg: dict, budget: Budget, force: bool) -> str:
    key = (cfg.get("steam_financial_api_key") or "").strip()
    projects = steam_projects(session)
    if not key or not projects:
        return "off"
    by_app = {int(p.steam_app_id): p.id for p in projects}
    st = kv_get(session, "steam_sales", {}) or {}
    pending: list[str] = st.get("pending") or []
    if not pending and _due(session, "steam_sales", STEAM_REFRESH_EVERY, force):
        dates, mark = steam_partner.changed_sales_dates(key, st.get("mark") or "0")
        if mark is None:
            raise steam_partner.SteamPartnerError(
                "empty GetChangedDatesForPartner response — the key has no access to sales data")
        if not st.get("mark"):
            first = shift(today_str(), -SALES_INITIAL_DAYS)
            dates = [d for d in dates if d >= first]
        pending, st["next_mark"] = sorted(set(dates), reverse=True), mark
    done = 0
    while pending and budget.left():
        day = pending[0]
        upsert_points(session, sales_rows(by_app, day, steam_partner.detailed_sales(key, day)))
        pending.pop(0)
        done += 1
        st["pending"] = pending
        kv_set(session, "steam_sales", st)
        time.sleep(CALL_PAUSE_S)
    if not pending and st.get("next_mark"):
        st["mark"], st["next_mark"] = st["next_mark"], None
    st["pending"] = pending
    kv_set(session, "steam_sales", st)
    return f"days processed: {done}" + (f", {len(pending)} more queued" if pending else "")


# ── followers (every social account + each game's Steam page) ────────────────

def _audience_jobs(session: Session, cfg: dict, force: bool) -> list[tuple[str, Callable]]:
    """(status key, fn(client) -> (rows, info)) for every connected channel that is due."""
    jobs = []
    for ch in audience.CHANNELS:
        if ch.ready(cfg) and _due(session, ch.key, AUDIENCE_EVERY, force):
            def run(c: httpx.Client, ch=ch):
                values, info = ch.fetch(cfg, c)
                return [{"project_id": 0, "metric": m, "value": v} for m, v in values.items()], info
            jobs.append((ch.key, run))
    games = [(p.id, p.name, int(p.steam_app_id)) for p in steam_projects(session)]
    if games and _due(session, audience.STEAM_KEY, AUDIENCE_EVERY, force):
        def run_steam(c: httpx.Client):
            rows, notes, errors = [], [], []
            for pid, name, appid in games:
                try:
                    n = audience.steam_followers(appid, c)
                    rows.append({"project_id": pid, "metric": "steam_followers", "value": n})
                    notes.append(f"{name}: {n}")
                except Exception as exc:
                    errors.append(f"{name}: {exc}")
            if errors and not rows:
                raise audience.AudienceError("; ".join(errors))
            return rows, "; ".join(notes + errors)
        jobs.append((audience.STEAM_KEY, run_steam))
    return jobs


def _with_client(fn: Callable):
    with httpx.Client(timeout=12, follow_redirects=True, headers={"User-Agent": "IndiePulse/1.0"}) as c:
        return fn(c)


def collect_audience(session: Session, cfg: dict, budget: Budget, force: bool = False) -> dict:
    """Read every due channel in parallel; store daily levels, timestamped samples and
    the all-platform total. A failing channel only marks its own status."""
    from app.core.redact import redact
    jobs = _audience_jobs(session, cfg, force)
    if not jobs:
        return {}
    pool = ThreadPoolExecutor(max_workers=min(AUDIENCE_WORKERS, len(jobs)))
    futures = {pool.submit(_with_client, fn): key for key, fn in jobs}
    done, _late = wait(futures, timeout=max(1.0, budget.seconds_left()))
    pool.shutdown(wait=False, cancel_futures=True)
    taken_at = datetime.utcnow()
    summary: dict = {}
    rows: list[dict] = []
    for fut, key in futures.items():
        if fut not in done:
            summary[key] = "error: no response"
            record_status(session, key, False, error="no response in the allotted time")
            continue
        try:
            got, info = fut.result()
        except Exception as exc:
            msg = redact(str(exc))[:300]
            logger.warning(f"audience {key} failed: {msg}")
            summary[key] = f"error: {msg}"
            record_status(session, key, False, error=msg)
            continue
        rows += got
        summary[key] = info
        record_status(session, key, True, info=info)
    if rows:
        if latest_sample_ts(session) is None:
            _seed_samples_from_daily(session)
        day = today_str()
        upsert_points(session, [{**r, "day": day, "source": "audience"} for r in rows])
        add_samples(session, rows, taken_at)
        rebuild_followers_total(session)
    return summary


def _seed_samples_from_daily(session: Session) -> None:
    """Once: turn the daily follower points collected before samples existed (Discord,
    YouTube) into samples at the time of that day's last read, for older publications."""
    old = session.exec(select(MetricPoint).where(MetricPoint.project_id == 0, MetricPoint.dim == "",
                                                 MetricPoint.metric.in_(audience.FOLLOWER_METRICS))).all()
    for p in old:
        if p.updated_at is not None:
            add_samples(session, [{"metric": p.metric, "value": p.value}], p.updated_at)


def rebuild_followers_total(session: Session) -> None:
    """followers_total per day = sum of every account's last known count. An account
    counts from its first read backwards too, so connecting one isn't "growth"."""
    points = session.exec(select(MetricPoint).where(MetricPoint.project_id == 0, MetricPoint.dim == "",
                                                    MetricPoint.metric.in_(audience.FOLLOWER_METRICS))
                          .order_by(MetricPoint.day)).all()
    if not points:
        return
    by_metric: dict[str, dict[str, float]] = {}
    for p in points:
        by_metric.setdefault(p.metric, {})[p.day] = p.value or 0
    current = {m: vals[min(vals)] for m, vals in by_metric.items()}
    rows = []
    for d in day_range(points[0].day, max(today_str(), points[-1].day)):
        for m, vals in by_metric.items():
            if d in vals:
                current[m] = vals[d]
        rows.append({"metric": "followers_total", "day": d, "value": sum(current.values()), "source": "audience"})
    upsert_points(session, rows)


def snapshot_before_publish(budget_s: float = PRE_PUBLISH_BUDGET_S) -> str:
    """Fresh follower counts right before a post goes out, so its report knows how
    many followers there were. Skipped when the last read is recent anyway."""
    from app.core.filelock import FileLock
    from app.db.session import engine
    with Session(engine) as session:
        last = latest_sample_ts(session)
        if last is not None and datetime.utcnow() - last < PRE_PUBLISH_FRESH:
            return "fresh"
        lock = FileLock("marketing_collect", timeout=0.5, stale=600)
        try:
            lock.acquire()
        except TimeoutError:
            return "busy"
        try:
            return str(collect_audience(session, load_settings(), Budget(budget_s), force=True))
        finally:
            lock.release()


# ── publications → daily views/reactions ─────────────────────────────────────

def collect_post_metrics(session: Session, cfg: dict, budget: Budget, force: bool) -> str:
    """post_views / post_reactions from the per-post counters (no network)."""
    from app.services.publications import rebuild_post_metrics
    return rebuild_post_metrics(session)


SOURCES: list[tuple[str, str, Callable]] = [
    ("posts", "Posts", collect_post_metrics),
]
STEAM_SOURCES: list[tuple[str, str, Callable]] = [
    ("steam_wishlists", "Steam: wishlists", collect_steam_wishlists),
    ("steam_sales", "Steam: sales", collect_steam_sales),
]
# statuses written elsewhere: steam_traffic.import_payload (the bookmarklet), collect_audience
LABELS = {source: label for source, label, _fn in SOURCES + STEAM_SOURCES}
LABELS["steam_traffic"] = "Steam: traffic"
LABELS.update(audience.LABELS)
AUDIENCE_KEYS = set(audience.LABELS)


def _run_sources(session: Session, sources: list, cfg: dict, budget: Budget, force: bool) -> dict:
    from app.services.marketing import evaluate_goals
    summary: dict = {}
    for source, _label, fn in sources:
        try:
            info = fn(session, cfg, budget, force)
            if info not in ("off", "skip"):
                record_status(session, source, True, info=info)
            summary[source] = info
        except Exception as exc:
            session.rollback()
            from app.core.redact import redact
            msg = redact(str(exc))[:300]
            logger.warning(f"marketing collect {source} failed: {msg}")
            record_status(session, source, False, error=msg)
            summary[source] = f"error: {msg}"
    achieved = evaluate_goals(session)
    if achieved:
        summary["achieved"] = [g.title for g in achieved]
    return summary


def collect_all(force: bool = False, budget_s: float = 25.0, steam_background: bool = True) -> dict:
    """Followers and publication stats within `budget_s` seconds, then Steam reports:
    on a background thread (the worker) or inline with the same budget."""
    from app.core.filelock import FileLock
    from app.db.session import engine

    lock = FileLock("marketing_collect", timeout=0.5, stale=600)
    try:
        lock.acquire()
    except TimeoutError:
        return {"skipped": "already running"}
    try:
        cfg = load_settings()
        budget = Budget(budget_s)
        with Session(engine) as session:
            summary = collect_audience(session, cfg, budget, force)
            summary.update(_run_sources(session, SOURCES, cfg, budget, force))
            if not steam_background:
                summary.update(_run_sources(session, STEAM_SOURCES, cfg, budget, force))
    finally:
        lock.release()
    if steam_background:
        summary["steam"] = "started" if start_steam_sync(force) else "already running"
    return summary


_steam_thread: Optional[threading.Thread] = None
_steam_guard = threading.Lock()


def steam_sync(force: bool = False, budget_s: float = STEAM_SYNC_BUDGET_S) -> dict:
    """Steam wishlists and sales in one go: refresh, then history until done or out of time."""
    from app.db.session import engine
    with Session(engine) as session:
        return _run_sources(session, STEAM_SOURCES, load_settings(), Budget(budget_s), force)


def start_steam_sync(force: bool = False) -> bool:
    """Run steam_sync on a background thread of this process, unless one is running.
    Only the worker calls it, so one thread per process is enough."""
    global _steam_thread
    with _steam_guard:
        if _steam_thread is not None and _steam_thread.is_alive():
            return False
        _steam_thread = threading.Thread(target=steam_sync, kwargs={"force": force},
                                         name="steam-sync", daemon=True)
        _steam_thread.start()
        return True


def source_statuses(session: Session) -> list[dict]:
    """For the home screen: last result of every collector that has run at least once."""
    out = []
    for source, label in LABELS.items():
        st = _status(session, source)
        if st:
            out.append({"source": source, "label": label, "audience": source in AUDIENCE_KEYS, **st})
    return out
