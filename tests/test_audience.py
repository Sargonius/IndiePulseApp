"""Followers of every account, publication reports (before → after) and post stats."""
import json
from datetime import datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import select

from app.models.models import AudienceSample, Brief, MetricPoint, Post, PostStatusEnum
from app.services import audience
from app.services import marketing as mk
from app.services import marketing_collect as mc
from app.services import publications as pubs
from tests.conftest import REAL_SNAPSHOT_BEFORE_PUBLISH


@pytest.fixture
def clean(session):
    """The test DB is shared: start without follower / publication numbers."""
    session.execute(delete(AudienceSample))
    session.execute(delete(MetricPoint).where(MetricPoint.metric.in_(
        audience.FOLLOWER_METRICS + ["followers_total", "steam_followers", "yt_views", *pubs.POST_METRICS])))
    session.execute(delete(Post))
    session.commit()
    return session


class Resp:
    def __init__(self, data, code=200, text=None):
        self._d, self.status_code = data, code
        self.text = text if text is not None else json.dumps(data)
        self.content = self.text.encode()

    def json(self):
        if self._d is None:
            raise ValueError("not json")
        return self._d


class FakeClient:
    """httpx.Client stand-in: routes GETs by URL substring."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        for part, resp in self.routes.items():
            if part in url:
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected GET {url}")


# ── fetchers ─────────────────────────────────────────────────────────────────

def test_fetchers_read_each_platform():
    vk = FakeClient({"groups.getById": Resp({"response": {"groups": [{"name": "ОколоГеймдев", "members_count": 4362}]}})})
    values, info = audience.vk({"vk_group_id": "club69792815", "vk_access_token": "t"}, vk)
    assert values == {"vk_members": 4362} and "4362" in info
    assert vk.calls[0][1]["params"]["group_id"] == "69792815"
    old_vk = FakeClient({"groups.getById": Resp({"response": [{"members_count": 7}]})})
    assert audience.vk({"vk_group_id": "1", "vk_access_token": "t"}, old_vk)[0] == {"vk_members": 7}
    err = FakeClient({"groups.getById": Resp({"error": {"error_code": 5, "error_msg": "auth failed"}})})
    with pytest.raises(audience.AudienceError, match="VK 5"):
        audience.vk({"vk_group_id": "1", "vk_access_token": "t"}, err)

    tg = FakeClient({"getChatMemberCount": Resp({"ok": True, "result": 87})})
    assert audience.telegram({"tg_bot_token": "b", "tg_chat_id_ru": "-100"}, tg)[0] == {"tg_subscribers": 87}

    fb = FakeClient({"graph.facebook.com": Resp({"name": "Page", "followers_count": 101, "fan_count": 99})})
    assert audience._facebook("ru")({"fb_ru_page_id": "1", "fb_ru_page_access_token": "t"}, fb)[0] == {"fb_ru_followers": 101}

    bsky = FakeClient({"getProfile": Resp({"followersCount": 35})})
    assert audience.bluesky({"bluesky_handle": "me.bsky.social"}, bsky)[0] == {"bsky_followers": 35}

    xml = ("<memberList><groupDetails><memberCount>930</memberCount></groupDetails>\n"
           "<memberCount>938</memberCount><members></members></memberList>")
    steam = FakeClient({"memberslistxml": Resp(None, text=xml)})
    assert audience.steam_followers(1156500, steam) == 938  # the live count, not the lagging one
    html = FakeClient({"memberslistxml": Resp(None, text="<!DOCTYPE html><html></html>")})
    with pytest.raises(audience.AudienceError, match="no Steam community"):
        audience.steam_followers(1, html)


def test_x_followers_come_from_users_me(monkeypatch):
    from app.services import tokens
    monkeypatch.setattr(tokens, "x_ensure_token", lambda force=False: "tok")
    c = FakeClient({"users/me": Resp({"data": {"username": "SargoniusGames",
                                              "public_metrics": {"followers_count": 325}}})})
    values, info = audience.x({}, c)
    assert values == {"x_followers": 325} and info == "@SargoniusGames: 325"
    assert c.calls[0][1]["headers"]["Authorization"] == "Bearer tok"


def test_discord_members_via_the_only_guild(clean, monkeypatch):
    def guild(url, kw):
        assert kw["params"]["with_counts"] == "true"
        return Resp({"name": "Sargonius", "approximate_member_count": 137})
    fake = FakeClient({"/users/@me/guilds": Resp([{"id": "555"}]), "/guilds/555": guild})
    monkeypatch.setattr(mc, "_with_client", lambda fn: fn(fake))
    monkeypatch.setattr(mc, "steam_projects", lambda s: [])
    summary = mc.collect_audience(clean, {"discord_bot_token": "x" * 20}, mc.Budget(5), force=True)
    assert "137" in summary["discord"]
    assert mk.level_on_or_before(clean, 0, "discord_members", mk.today_str()).value == 137
    from app.services.settings import load_settings
    assert load_settings()["discord_guild_id"] == "555"
    sample = clean.exec(select(AudienceSample).where(AudienceSample.metric == "discord_members")).one()
    assert sample.value == 137 and sample.project_id == 0


# ── collection ───────────────────────────────────────────────────────────────

def _channel(key, metric, fetch):
    return audience.Channel(key, key.upper(), metric, (key,), lambda cfg: True, fetch)


def test_collect_audience_isolates_failures_and_skips_fresh_channels(clean, monkeypatch, project):
    def boom(cfg, c):
        raise RuntimeError("token expired")
    calls = []

    def ok(cfg, c):
        calls.append(1)
        return {"x_followers": 325}, "@me: 325"
    monkeypatch.setattr(audience, "CHANNELS", [_channel("x", "x_followers", ok), _channel("tg", "tg_subscribers", boom)])
    game = project(name="Followed game", steam_app_id=4242)
    monkeypatch.setattr(mc, "steam_projects", lambda s: [game])
    monkeypatch.setattr(audience, "steam_followers", lambda appid, c: {4242: 938}[appid])
    monkeypatch.setattr(mc, "_with_client", lambda fn: fn(None))

    summary = mc.collect_audience(clean, {}, mc.Budget(5))
    assert summary["x"] == "@me: 325" and summary["tg"].startswith("error: token expired")
    assert summary["steam_followers"] == "Followed game: 938"
    statuses = {s["source"]: s for s in mc.source_statuses(clean)}
    assert statuses["x"]["ok"] and statuses["x"]["audience"] and not statuses["tg"]["ok"]
    assert mk.level_on_or_before(clean, game.id, "steam_followers", mk.today_str()).value == 938
    assert mk.level_on_or_before(clean, 0, "followers_total", mk.today_str()).value == 325

    # read less than 2 hours ago → not due; the failed channel is retried
    summary = mc.collect_audience(clean, {}, mc.Budget(5))
    assert "x" not in summary and "tg" in summary and len(calls) == 1


def test_followers_total_counts_a_new_account_from_its_first_read(clean):
    mk.upsert_points(clean, [{"metric": "discord_members", "day": d, "value": v}
                             for d, v in {"2026-03-01": 10, "2026-03-02": 11, "2026-03-03": 12}.items()])
    mk.upsert_points(clean, [{"metric": "vk_members", "day": "2026-03-03", "value": 100},
                             {"metric": "vk_members", "day": "2026-03-05", "value": 103}])
    mc.rebuild_followers_total(clean)
    total = dict(mk.daily_series(clean, 0, "followers_total", "2026-03-01", "2026-03-05"))
    # VK connected on the 3rd: counted as 100 before that, so it isn't growth
    assert total == {"2026-03-01": 110, "2026-03-02": 111, "2026-03-03": 112,
                     "2026-03-04": 112, "2026-03-05": 115}


def test_snapshot_before_publish_skips_when_a_read_is_recent(clean, monkeypatch):
    runs = []
    monkeypatch.setattr(mc, "collect_audience", lambda s, cfg, budget, force=False: runs.append(force) or {"x": "ok"})
    assert REAL_SNAPSHOT_BEFORE_PUBLISH() == "{'x': 'ok'}" and runs == [True]
    mk.add_samples(clean, [{"metric": "x_followers", "value": 1}], datetime.utcnow() - timedelta(minutes=5))
    assert REAL_SNAPSHOT_BEFORE_PUBLISH() == "fresh" and runs == [True]


def test_publishing_reads_followers_first(make_post, monkeypatch):
    from app.jobs import tasks
    from app.platforms import PUBLISHERS
    order = []
    monkeypatch.setattr(mc, "snapshot_before_publish", lambda: order.append("snapshot"))
    monkeypatch.setitem(PUBLISHERS, "bluesky", lambda s, post, job: order.append("publish"))
    post_id, job_id = make_post("bluesky")
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert order == ["snapshot", "publish"]


# ── publications ─────────────────────────────────────────────────────────────

def _publication(session, platforms, t0, project_id=None, text="Wasteland Life devlog", metrics=None):
    brief = Brief(user_id=1, type="post", text_ru=text, project_id=project_id)
    session.add(brief)
    session.commit()
    for i, plat in enumerate(platforms):
        session.add(Post(user_id=1, brief_id=brief.id, platform=plat, status=PostStatusEnum.done,
                         published_at=t0 + timedelta(minutes=i),
                         metrics_json=json.dumps((metrics or {}).get(plat) or {})))
    session.commit()
    return brief


def test_engagement_maps_every_platform_naming():
    e = pubs.engagement('{"retweet": 2, "reply": 1, "like": 5, "quote": 1, "views": 300}')
    assert (e["likes"], e["reposts"], e["comments"], e["views"], e["reactions"]) == (5, 2, 1, 300, 9)
    e = pubs.engagement({"likes": None, "error": "x"})
    assert e["reactions"] is None and not e["has"] and e["error"] == "x"


def test_publication_report_before_and_after(clean, project):
    game = project(name="Wasteland Life", steam_app_id=3000001)
    t0 = datetime(2026, 9, 20, 15, 0)
    brief = _publication(clean, ["x", "vk"], t0, project_id=game.id,
                         metrics={"x": {"like": 5, "retweet": 1}, "vk": {"likes": 3, "views": 250}})
    samples = {
        "x_followers": [(-1, 100), (2, 104), (30, 110)],
        "vk_members": [(-1, 50), (30, 51)],
        "tg_subscribers": [(-1, 20), (30, 25)],        # not posted there
        "bsky_followers": [(-60, 5)],                  # too old to count as "before", no read after
    }
    for metric, reads in samples.items():
        for h, v in reads:
            mk.add_samples(clean, [{"metric": metric, "value": v}], t0 + timedelta(hours=h))
    mk.add_samples(clean, [{"project_id": game.id, "metric": "steam_followers", "value": 40}], t0 - timedelta(hours=1))
    mk.add_samples(clean, [{"project_id": game.id, "metric": "steam_followers", "value": 43}], t0 + timedelta(hours=30))
    other = _publication(clean, ["tg"], t0 + timedelta(hours=10), text="Another post")

    r = pubs.report(clean, brief, now=t0 + timedelta(hours=40))
    rows = {row["metric"]: row for row in r["rows"]}
    assert set(rows) == {"x_followers", "vk_members", "tg_subscribers", "steam_followers"}
    x = rows["x_followers"]
    assert x["posted"] and x["before"] == 100 and x["now"] == 110 and x["now_delta"] == 10
    assert x["checkpoints"][0] == {"label": "+24 h", "value": 104, "delta": 4}
    assert x["checkpoints"][1]["pending"] and x["checkpoints"][2]["pending"]
    assert not rows["tg_subscribers"]["posted"]
    assert r["gain_posted"] == 11 and r["gain_all"] == 16 and r["gain_steam"] == 3
    assert r["window_elapsed"] == "40 h" and not r["window_complete"]
    assert pubs.fmt_elapsed(timedelta(minutes=27)) == "27 min"
    assert pubs.fmt_elapsed(timedelta(hours=52)) == "2 d 4 h"
    assert [o["brief"].id for o in r["others"]] == [other.id]
    assert r["total"]["reactions"] == 9 and r["total"]["views"] == 250

    listed = pubs.with_gains(clean, pubs.published(clean, t0 - timedelta(days=1)), now=t0 + timedelta(hours=40))
    mine = next(p for p in listed if p["brief"].id == brief.id)
    assert mine["gain"]["posted"] == 11 and mine["gain"]["all"] == 16 and mine["gain"]["steam"] == 3
    assert mine["gain"]["counted"] == ["VK", "X"] and mine["gain"]["missing"] == []
    assert mine["platform_labels"] == ["VK", "X"]


def test_report_without_reads_before_the_post(clean):
    t0 = datetime(2026, 9, 1, 12, 0)
    brief = _publication(clean, ["x"], t0)
    mk.add_samples(clean, [{"metric": "x_followers", "value": 300}], t0 + timedelta(days=20))
    r = pubs.report(clean, brief, now=t0 + timedelta(days=21))
    assert not r["has_before"] and r["gain_posted"] is None
    assert r["rows"][0]["before"] is None and r["rows"][0]["now"] == 300


def test_post_metrics_by_publish_day(clean, project):
    game = project(name="Views game")
    now = datetime.utcnow().replace(hour=12)
    _publication(clean, ["vk", "tg"], now, project_id=game.id,
                 metrics={"vk": {"likes": 4, "views": 100, "comments": 1}, "tg": {"views": 50}})
    _publication(clean, ["x"], now, metrics={"x": {"like": 2}})
    mc.collect_post_metrics(clean, {}, mc.Budget(5), False)
    day = pubs.local_day(now)
    assert mk.window_sum(clean, 0, "post_views", day, day)[0] == 150
    assert mk.window_sum(clean, 0, "post_reactions", day, day)[0] == 7
    assert mk.window_sum(clean, game.id, "post_views", day, day)[0] == 150
    assert mk.window_sum(clean, game.id, "post_reactions", day, day)[0] == 5
    # a rebuild replaces the numbers instead of adding to them
    mc.collect_post_metrics(clean, {}, mc.Budget(5), False)
    assert mk.window_sum(clean, 0, "post_views", day, day)[0] == 150


def test_publication_pages_and_home(client, clean, project):
    game = project(name="Report game")
    t0 = datetime.utcnow() - timedelta(hours=5)
    brief = _publication(clean, ["x"], t0, project_id=game.id, text="Devlog #7", metrics={"x": {"like": 3}})
    mk.add_samples(clean, [{"metric": "x_followers", "value": 320}], t0 - timedelta(minutes=1))
    mk.add_samples(clean, [{"metric": "x_followers", "value": 325}], t0 + timedelta(hours=2))
    mk.upsert_points(clean, [{"metric": "x_followers", "day": mk.today_str(), "value": 325}])
    mc.rebuild_followers_total(clean)

    page = client.get(f"/publications/{brief.id}")
    assert page.status_code == 200 and "Devlog #7" in page.text and "Where the post went out" in page.text
    assert "+5" in page.text
    assert client.get("/publications/999999").status_code == 404
    listing = client.get(f"/publications?project={game.id}&days=7")
    assert listing.status_code == 200 and "Devlog #7" in listing.text
    home = client.get("/")
    assert home.status_code == 200 and 'id="audience"' in home.text and "Devlog #7" in home.text


def test_goal_on_all_publications_is_allowed(client, session):
    r = client.post("/api/goals", data={"metric": "post_views", "target": "1000", "project_id": "0"},
                    follow_redirects=False)
    assert r.status_code == 303
    r = client.post("/api/goals", data={"metric": "steam_followers", "target": "10", "project_id": "0"},
                    follow_redirects=False)
    assert r.status_code == 400
