"""Marketing layer: metric storage, goal progress, Steam/Discord collectors, pages."""
import json

import pytest
from sqlmodel import select

from app.models.models import Brief, Goal, GoalLink, MetricPoint, Project
from app.services import marketing as mk
from app.services import marketing_collect as mc
from app.services import steam_partner
from tests.conftest import reload


def _flow(session, pid, metric, values: dict, **kw):
    mk.upsert_points(session, [{"project_id": pid, "metric": metric, "day": d, "value": v, **kw}
                               for d, v in values.items()])


# ── storage & series ─────────────────────────────────────────────────────────

def test_upsert_overwrites_same_day(session, project):
    p = project()
    _flow(session, p.id, "steam_wishlist_adds", {"2026-01-01": 3})
    _flow(session, p.id, "steam_wishlist_adds", {"2026-01-01": 5})
    rows = session.exec(select(MetricPoint).where(MetricPoint.project_id == p.id)).all()
    assert [(r.day, r.value) for r in rows] == [("2026-01-01", 5.0)]


def test_weekly_manual_entry_is_spread_over_its_days(session, project):
    p = project()
    mk.upsert_points(session, [{"project_id": p.id, "metric": "steam_visits", "day": "2026-01-07",
                                "value": 70, "span_days": 7}])
    series = dict(mk.daily_series(session, p.id, "steam_visits", "2026-01-01", "2026-01-08"))
    assert series["2026-01-01"] == pytest.approx(10)
    assert series["2026-01-07"] == pytest.approx(10)
    assert series["2026-01-08"] is None
    # a window that covers only part of the week gets the prorated share
    assert mk.window_sum(session, p.id, "steam_visits", "2026-01-05", "2026-01-10")[0] == pytest.approx(30)


# ── goals ────────────────────────────────────────────────────────────────────

def test_flow_goal_progress_rates_and_eta(session, project):
    p = project()
    # 14 days before the start: 1/day; after the start: 4/day
    _flow(session, p.id, "steam_wishlist_adds", {mk.shift("2026-03-01", -i): 1 for i in range(1, 15)})
    _flow(session, p.id, "steam_wishlist_adds", {mk.shift("2026-03-01", i): 4 for i in range(5)})
    g = Goal(title="+50", project_id=p.id, metric="steam_wishlist_adds", target=50, start_day="2026-03-01")
    session.add(g)
    session.commit()
    gp = mk.goal_progress(session, g, today="2026-03-05")
    assert gp.current == 20 and gp.target == 50
    assert gp.pct == pytest.approx(0.4)
    assert gp.rate_now == pytest.approx(4) and gp.rate_before == pytest.approx(1)
    assert gp.eta_day == "2026-03-13"  # 30 left at 4/day → 8 more days
    assert not gp.reached


def test_level_goals_delta_and_total(session):
    base = {"2026-04-01": 100, "2026-04-05": 104, "2026-04-09": 111}
    mk.upsert_points(session, [{"project_id": 0, "metric": "discord_members", "day": d, "value": v}
                               for d, v in base.items()])
    delta = Goal(title="+10 discord", metric="discord_members", target=10, start_day="2026-04-02")
    total = Goal(title="reach 120", metric="discord_members", mode="total", target=120, start_day="2026-04-02")
    session.add(delta)
    session.add(total)
    session.commit()
    d = mk.goal_progress(session, delta, today="2026-04-09")
    assert d.baseline == 100 and d.current == 11 and d.reached
    assert mk.crossing_day(session, delta, today="2026-04-09") == "2026-04-09"
    t = mk.goal_progress(session, total, today="2026-04-09")
    assert t.target == 20 and t.current == 11 and not t.reached


def test_evaluate_goals_marks_the_crossing_day(session, project):
    p = project()
    _flow(session, p.id, "steam_units", {"2026-05-01": 2, "2026-05-02": 5, "2026-05-03": 1})
    g = Goal(title="sell 6", project_id=p.id, metric="steam_units", target=6, start_day="2026-05-01")
    session.add(g)
    session.commit()
    done = mk.evaluate_goals(session, today="2026-05-03")
    assert g.id in [x.id for x in done]
    g = reload(Goal, g.id)
    assert g.status == "achieved" and g.achieved_day == "2026-05-02"


# ── Steam parsing ────────────────────────────────────────────────────────────

WISHLIST_RESP = {
    "appid": 1156500, "date": "2026-06-10", "app_min_date": "2026-06-08",
    "wishlist_summary": {"wishlist_adds": 12, "wishlist_deletes": 2, "wishlist_purchases": 1},
    "country_summary": [
        {"country_code": "JP", "summary_actions": {"wishlist_adds": 5, "wishlist_deletes": 1}},
        {"country_code": "DE", "summary_actions": {"wishlist_adds": 0}},
    ],
    "language_summary": [{"language": 7, "language_name": "Japanese", "summary_actions": {"wishlist_adds": 4}}],
}


def test_wishlist_rows_totals_net_and_breakdowns():
    rows = {(r["metric"], r.get("dim", "")): r["value"] for r in mc.wishlist_rows(7, "2026-06-10", WISHLIST_RESP)}
    assert rows[("steam_wishlist_adds", "")] == 12
    assert rows[("steam_wishlist_net", "")] == 10
    assert rows[("steam_wishlist_gifts", "")] == 0
    assert rows[("steam_wishlist_adds", "country:JP")] == 5
    assert rows[("steam_wishlist_net", "country:JP")] == 4
    assert ("steam_wishlist_adds", "country:DE") not in rows  # zero rows are skipped
    assert rows[("steam_wishlist_adds", "lang:japanese")] == 4


def test_sales_rows_sum_by_primary_app_and_fill_zero_days():
    items = [
        {"primary_appid": 100, "appid": 0, "net_units_sold": 2, "net_sales_usd": "9.98", "country_code": "jp"},
        {"primary_appid": 100, "net_units_sold": 1, "net_sales_usd": "4.99", "country_code": "US"},
        {"primary_appid": 999, "net_units_sold": 5, "net_sales_usd": "50"},  # someone else's app
    ]
    rows = mc.sales_rows({100: 1, 200: 2}, "2026-06-10", items)
    got = {(r["project_id"], r["metric"], r.get("dim", "")): r["value"] for r in rows}
    assert got[(1, "steam_units", "")] == 3
    assert got[(1, "steam_units", "country:JP")] == 2
    assert got[(1, "steam_revenue_usd", "")] == pytest.approx(14.97)
    assert got[(2, "steam_units", "")] == 0  # no sales that day → explicit zero
    assert not any(k[0] == 999 for k in got)


def test_detailed_sales_follows_max_id(monkeypatch):
    pages = {0: {"results": [{"id": 1}, {"id": 2}], "max_id": "5"}, 5: {"results": [{"id": 3}], "max_id": "9"},
             9: {"results": [], "max_id": "9"}}
    calls = []

    def fake_get(method, key, params):
        calls.append((params["date"], params["highwatermark_id"]))
        return pages[params["highwatermark_id"]]
    monkeypatch.setattr(steam_partner, "_get", fake_get)
    assert [i["id"] for i in steam_partner.detailed_sales("k", "2026/06/10")] == [1, 2, 3]
    assert calls[0] == ("2026-06-10", 0)  # slashes from GetChangedDatesForPartner are normalised


# ── collectors ───────────────────────────────────────────────────────────────

def test_wishlist_collector_refreshes_then_backfills_and_builds_balance(session, project, set_settings, monkeypatch):
    from datetime import datetime
    p = project(name="WL game", steam_app_id=424242)
    set_settings(steam_financial_api_key="k" * 12)
    latest = datetime.utcnow().date().isoformat()  # the collector works in GMT days
    min_date = mk.shift(latest, -5)
    seen = []

    def fake_report(key, appid, day):
        if appid == 424242:  # other tests' Steam projects share the DB
            seen.append(day)
        return {"app_min_date": min_date, "wishlist_summary": {"wishlist_adds": 2, "wishlist_deletes": 1}}
    monkeypatch.setattr(mc.steam_partner, "wishlist_report", fake_report)
    monkeypatch.setattr(mc, "CALL_PAUSE_S", 0)
    info = mc.collect_steam_wishlists(session, {"steam_financial_api_key": "k" * 12}, mc.Budget(10), force=True)
    assert "history complete" in info
    # the latest days first (fetched in parallel, so in any order), then history
    assert set(seen[:3]) == {latest, mk.shift(latest, -1), mk.shift(latest, -2)}
    assert sorted(seen) == mk.day_range(min_date, latest)  # every day exactly once
    bal = mk.level_on_or_before(session, p.id, "steam_wishlist_balance", latest)
    assert bal.value == 6  # 6 days × (2 adds − 1 delete)


def test_collect_all_isolates_failing_sources(session, set_settings, monkeypatch):
    set_settings(discord_bot_token="t" * 20)

    def boom(*a, **kw):
        raise RuntimeError("steam is down")
    monkeypatch.setattr(mc, "_audience_jobs", lambda *a: [])
    monkeypatch.setattr(mc, "SOURCES", [("discord", "Discord", lambda s, cfg, b, f: "«srv»: 42")])
    monkeypatch.setattr(mc, "STEAM_SOURCES", [("steam_wishlists", "Steam: wishlists", boom)])
    summary = mc.collect_all(force=True, budget_s=5, steam_background=False)
    assert summary["steam_wishlists"].startswith("error: steam is down")
    assert summary["discord"] == "«srv»: 42"
    statuses = {s["source"]: s for s in mc.source_statuses(session)}
    assert statuses["steam_wishlists"]["ok"] is False and statuses["discord"]["ok"] is True


# ── publications & pages ─────────────────────────────────────────────────────

def test_post_form_stores_project_and_goal(client, session, project):
    p = project(name="Tagged")
    g = Goal(title="patch", project_id=p.id, metric="steam_units", target=5, start_day=mk.today_str())
    session.add(g)
    session.commit()
    r = client.post("/create/post", data={"text_ru": "Патч вышел", "project_id": str(p.id), "goal_id": str(g.id)})
    assert r.status_code == 200
    brief = session.exec(select(Brief).where(Brief.text_ru == "Патч вышел")).first()
    assert brief.project_id == p.id
    assert session.exec(select(GoalLink).where(GoalLink.goal_id == g.id, GoalLink.brief_id == brief.id)).first()


def test_goal_api_validation_and_pages(client, session, project):
    p = project(name="Pages game", steam_app_id=31337, stage="released")
    # project metric without a project is refused
    r = client.post("/api/goals", data={"metric": "steam_units", "target": "10", "project_id": "0"},
                    follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/api/goals", data={"metric": "steam_units", "target": "10", "project_id": str(p.id)},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/goals/")
    goal_url = r.headers["location"]
    r = client.post("/api/metrics/manual", data={"project_id": str(p.id), "metric": "steam_visits",
                                                 "start_day": "2026-02-01", "end_day": "2026-02-07",
                                                 "value": "140", "next": "https://evil.example/"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # external "next" ignored
    pt = session.exec(select(MetricPoint).where(MetricPoint.project_id == p.id,
                                                MetricPoint.metric == "steam_visits")).first()
    assert pt.day == "2026-02-07" and pt.span_days == 7 and pt.value == 140
    home = client.get("/")
    assert home.status_code == 200 and "Pages game" in home.text and "Released" in home.text
    assert client.get("/goals").status_code == 200
    page = client.get(goal_url)
    assert page.status_code == 200 and "goal_chart_data" in page.text
    assert client.get("/goals/999999").status_code == 404
    guide = client.get("/guide")
    assert guide.status_code == 200 and 'id="pb-nopage"' in guide.text


def test_background_steam_sync_runs_one_thread_at_a_time(monkeypatch):
    import threading
    release, calls = threading.Event(), []

    def fake_sync(force=False):
        calls.append(force)
        release.wait(5)
    monkeypatch.setattr(mc, "steam_sync", fake_sync)
    monkeypatch.setattr(mc, "_steam_thread", None)
    assert mc.start_steam_sync(force=True) is True
    assert mc.start_steam_sync() is False  # still running
    release.set()
    mc._steam_thread.join(5)
    assert calls == [True]
    assert mc.start_steam_sync() is True  # finished → can start again
    mc._steam_thread.join(5)


def test_steam_projects_put_active_games_first(session, project):
    paused = project(name="Paused game", steam_app_id=515151, status="paused")
    active = project(name="Active game", steam_app_id=525252)
    order = [p.id for p in mc.steam_projects(session) if p.id in (paused.id, active.id)]
    assert order == [active.id, paused.id]


# ── Steam traffic (bookmarklet) ──────────────────────────────────────────────

TRAFFIC_CSV = (
    '"Page / Category","Page / Feature",Impressions,Visits,"Owner Impressions","Owner Visits"\n'
    '"Direct Search Results","Search Results",120,6,0,0\n'
    '"Tag Page",(Other),30,3,1,0\n'
    '"Bot Traffic",(Other),0,9,0,0\n'
    '"Bot Traffic","Browse Search Results",0,4,0,0\n'
    '"Direct Navigation",(Other),0,20,0,0\n'
    '"External Website",Google,0,5,0,0\n'
    '"External Website",reddit,0,2,0,0\n'
    'Country,US,90,25,0,0\n'
    'Country,JP,40,7,0,0\n'
)


def test_parse_navtraffic_csv_keeps_bots_apart():
    from app.services import steam_traffic as st
    t = st.parse_navtraffic_csv(TRAFFIC_CSV)
    assert t["impressions"] == 150  # country rows repeat the same traffic — not added twice
    assert t["visits"] == 36 and t["bot_visits"] == 13
    assert t["sources"]["External Website"] == 7 and "Bot Traffic" not in t["sources"]
    assert t["referrers"] == {"google": 5, "reddit": 2}
    assert t["countries"] == {"US": 25, "JP": 7}
    with pytest.raises(ValueError):
        st.parse_navtraffic_csv("<html>login</html>")


def _traffic_payload(token, rows):
    import base64
    import gzip
    raw = gzip.compress(json.dumps({"v": 1, "token": token, "days": 2, "rows": rows}).encode())
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_traffic_import_needs_the_token_and_replaces_manual_weeks(client, session, project):
    from app.services import steam_traffic as st
    p = project(name="Traffic game", steam_app_id=636363)
    token = st.sync_token()
    # a manual weekly entry over the same days must not be counted on top
    mk.upsert_points(session, [{"project_id": p.id, "metric": "steam_visits", "day": "2026-09-26",
                                "value": 700, "span_days": 7, "source": "manual"}])
    rows = [{"appid": 636363, "day": "2026-09-25", "csv": TRAFFIC_CSV},
            {"appid": 636363, "day": "2026-09-26", "csv": TRAFFIC_CSV},
            {"appid": 999001, "day": "2026-09-26", "csv": TRAFFIC_CSV}]  # not a project
    bad = client.post("/api/steam/traffic/import", content=_traffic_payload("wrong", rows),
                      headers={"Content-Type": "text/plain"})
    assert bad.status_code == 403
    r = client.post("/api/steam/traffic/import", content=_traffic_payload(token, rows),
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and "Traffic game" in r.text and "999001" in r.text
    series = dict(mk.daily_series(session, p.id, "steam_visits", "2026-09-20", "2026-09-26"))
    assert series["2026-09-26"] == 36 and series["2026-09-25"] == 36
    assert series["2026-09-24"] is None  # the manual week is gone
    assert mk.window_sum(session, p.id, "steam_visits", "2026-09-25", "2026-09-26", "bot")[0] == 26
    assert mk.window_sum(session, p.id, "steam_visits", "2026-09-26", "2026-09-26", "ref:reddit")[0] == 2
    assert st.last_traffic_day(session, p.id) == "2026-09-26"
    # re-importing a day overwrites it instead of adding
    client.post("/api/steam/traffic/import", content=_traffic_payload(token, rows[1:2]),
                headers={"Content-Type": "text/plain"})
    assert mk.window_sum(session, p.id, "steam_visits", "2026-09-26", "2026-09-26")[0] == 36
    # pages
    page = client.get("/steam/traffic")
    assert page.status_code == 200 and "javascript:" in page.text and "Traffic game" in page.text
    assert client.get("/steam/traffic/import").status_code == 200
    home = client.get("/")
    assert "Page visits" in home.text and "apps/navtrafficstats/636363" in home.text


def test_traffic_import_rejects_cross_site_posts(client):
    r = client.post("/api/steam/traffic/import", content="x",
                    headers={"Content-Type": "text/plain", "Origin": "https://partner.steamgames.com"})
    assert r.status_code == 403 and "cross-site" in r.text


def test_sync_plan_asks_for_missing_days(session, project):
    from app.services import steam_traffic as st
    p = project(name="Plan game", steam_app_id=747474)
    mk.upsert_points(session, [{"project_id": p.id, "metric": "steam_visits", "day": "2026-09-20",
                                "value": 5, "source": "steam_traffic"}])
    plan = st.sync_plan(session, today="2026-09-27")
    assert 747474 in plan["apps"]
    if all(v for v in plan["last"].values()):  # other tests' games may be unsynced → first-sync window
        assert plan["days"] >= 8
    assert "#indiepulse:days=" in st.steamworks_sync_url(session)


def test_bookmarklet_is_valid_javascript(tmp_path):
    import shutil
    import subprocess
    from urllib.parse import unquote
    from app.services import steam_traffic as st
    url = st.bookmarklet("http://127.0.0.1:8000/", "tok_123", [1156500, 2847510])
    import re
    assert url.startswith("javascript:") and not re.search(r"__[A-Z]+__", unquote(url))  # placeholders filled
    src = unquote(url[len("javascript:"):])
    assert '"http://127.0.0.1:8000"' in src and '"tok_123"' in src and "[1156500, 2847510]" in src
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    f = tmp_path / "bm.js"
    f.write_text(src, encoding="utf-8")
    check = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
    assert check.returncode == 0, check.stderr


def test_steam_links_for_goal_metrics():
    from app.services import steam_links
    assert steam_links.metric_link("steam_wishlist_net", 42)["url"].endswith("/app/wishlist/42/")
    assert "navtrafficstats/42" in steam_links.metric_link("steam_visits", 42)["url"]
    assert steam_links.metric_link("discord_members", 42) is None
    assert steam_links.metric_link("steam_units", None) is None
