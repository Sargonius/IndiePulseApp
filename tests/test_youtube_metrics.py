"""YouTube stats: explicit errors instead of blank counters, batched videos.list calls,
the 30-min-then-daily schedule and the backfill."""
import json
import uuid
from datetime import datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from loguru import logger

from app.jobs import tasks
from app.models.models import Post, PostStatusEnum
from app.services import tokens
from app.services.settings import load_settings
from tests.conftest import reload

BLANK = {"likes": None, "comments": None, "views": None}  # what the bug left behind


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def _item(vid, views=10, likes=2, comments=1, **status):
    return {"id": vid,
            "statistics": {"viewCount": str(views), "likeCount": str(likes), "commentCount": str(comments)},
            "status": {"privacyStatus": "public", "uploadStatus": "processed", **status}}


class FakeYouTubeAPI:
    """videos.list behind httpx.MockTransport; `videos` is what the channel has."""

    def __init__(self):
        self.videos: dict = {}
        self.calls: list = []        # (ids, bearer token) per videos.list request
        self.refreshes: list = []    # `force` of every yt_access_token() call
        self.token = "tok-1"
        self.reject_token = None     # a bearer the API answers with 401
        self.fail = None             # an httpx.Response returned for every call

    def access_token(self, force=False):
        self.refreshes.append(force)
        if force:
            self.token = "tok-2"
        return self.token

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path != "/youtube/v3/videos":
            return httpx.Response(404, json={"error": "not faked"})
        ids = request.url.params["id"].split(",")
        bearer = request.headers["Authorization"].removeprefix("Bearer ")
        self.calls.append((ids, bearer))
        if bearer == self.reject_token:
            return httpx.Response(401, json={"error": {"code": 401, "status": "UNAUTHENTICATED",
                                                       "message": "Request had invalid authentication credentials."}})
        if self.fail is not None:
            return self.fail
        items = [self.videos[v] for v in ids if v in self.videos]
        return httpx.Response(200, json={"kind": "youtube#videoListResponse", "items": items,
                                         "pageInfo": {"totalResults": len(items), "resultsPerPage": len(items)}})


def _route_httpx(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **kw: real_client(*a, transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def yt_api(monkeypatch):
    api = FakeYouTubeAPI()
    monkeypatch.setattr(tokens, "yt_access_token", api.access_token)
    _route_httpx(monkeypatch, api.handle)
    return api


@pytest.fixture
def yt_post(make_post, session):
    """A published YouTube post; returns (post_id, video_id)."""
    def _make(days_ago=1.0, metrics=None, status=PostStatusEnum.done, with_id=True):
        vid = uuid.uuid4().hex[:11] if with_id else None
        post_id, _ = make_post("yt", status=status)
        p = session.get(Post, post_id)
        p.platform_post_id = vid
        p.platform_url = f"https://www.youtube.com/watch?v={vid}" if vid else None
        p.published_at = datetime.utcnow() - timedelta(days=days_ago)
        if metrics is not None:
            p.metrics_json = json.dumps(metrics)
        session.add(p)
        session.commit()
        return post_id, vid
    return _make


def _metrics(post_id) -> dict:
    return json.loads(reload(Post, post_id).metrics_json)


# ── fetch: explicit errors ───────────────────────────────────────────────────

def test_video_missing_from_response_is_an_error_not_blank_counters(yt_post, yt_api):
    # HTTP 200 with empty `items` used to be stored as all-None counters, silently
    post_id, vid = yt_post(metrics=BLANK)
    logs = []
    sink = logger.add(lambda msg: logs.append(str(msg)), level="WARNING")
    try:
        tasks.refresh_metrics_for_post.call_local(post_id)
    finally:
        logger.remove(sink)
    m = _metrics(post_id)
    assert m["error"].startswith("yt_video_not_found")
    assert "views" not in m and m["checked_at"]
    assert any("missing=" in line and vid in line and "'items': []" in line for line in logs)


def test_batch_uses_one_call_per_50_videos(yt_post, yt_api):
    posts = [yt_post(metrics=BLANK) for _ in range(120)]
    yt_api.videos = {vid: _item(vid, views=i) for i, (_, vid) in enumerate(posts)}
    rejected = posts[5][1]
    yt_api.videos[rejected] = {"id": rejected, "status": {"privacyStatus": "public", "uploadStatus": "rejected",
                                                          "rejectionReason": "duplicate"}}

    assert tasks.refresh_youtube_metrics.call_local([pid for pid, _ in posts]) == "videos=120 failed=1"
    assert [len(ids) for ids, _ in yt_api.calls] == [50, 50, 20]
    m = _metrics(posts[7][0])
    assert (m["views"], m["likes"], m["comments"]) == (7, 2, 1) and "error" not in m
    assert _metrics(posts[5][0])["error"] == \
        "yt_no_statistics (privacyStatus=public, uploadStatus=rejected, rejectionReason=duplicate)"


def test_rejected_token_is_refreshed_once_and_retried(yt_post, yt_api):
    post_id, vid = yt_post()
    yt_api.videos = {vid: _item(vid, views=42)}
    yt_api.reject_token = "tok-1"  # revoked, although its stored expiry still looks fine
    tasks.refresh_metrics_for_post.call_local(post_id)
    assert yt_api.refreshes == [False, True]
    assert [bearer for _, bearer in yt_api.calls] == ["tok-1", "tok-2"]
    assert _metrics(post_id)["views"] == 42


def test_dead_refresh_token_is_recorded_and_redacted(yt_post, monkeypatch, set_settings):
    set_settings(yt_client_secret="GOCSPX-very-secret-value")

    def dead(force=False):
        raise tokens.YouTubeAuthError("yt_refresh_failed: invalid_grant (client GOCSPX-very-secret-value)",
                                      reauth=True)
    monkeypatch.setattr(tokens, "yt_access_token", dead)
    post_id, _ = yt_post(metrics=BLANK)
    tasks.refresh_youtube_metrics.call_local([post_id])
    m = _metrics(post_id)
    assert m["error"].startswith("yt_reauth_needed: yt_refresh_failed: invalid_grant")
    assert "GOCSPX" not in reload(Post, post_id).metrics_json


def test_quota_error_keeps_previous_numbers(yt_post, yt_api):
    post_id, _ = yt_post(metrics={"likes": 3, "comments": 1, "views": 500})
    yt_api.fail = httpx.Response(403, json={"error": {
        "code": 403, "message": "The request cannot be completed because you have exceeded your quota.",
        "errors": [{"reason": "quotaExceeded"}]}})
    tasks.refresh_youtube_metrics.call_local([post_id])
    m = _metrics(post_id)
    assert (m["views"], m["likes"], m["stale"]) == (500, 3, True)
    assert m["error"].startswith("yt_metrics_http_403 quotaExceeded")


# ── schedule ─────────────────────────────────────────────────────────────────

def test_every_run_for_a_week_then_daily_until_the_limit(yt_post, session, set_settings):
    now = datetime.utcnow()

    def checked(hours_ago):
        return {"views": 1, "checked_at": (now - timedelta(hours=hours_ago)).isoformat(timespec="seconds")}

    fresh, _ = yt_post(days_ago=2, metrics=checked(0.1))
    checked_recently, _ = yt_post(days_ago=30, metrics=checked(2))
    checked_yesterday, _ = yt_post(days_ago=30, metrics=checked(30))
    never_checked, _ = yt_post(days_ago=60, metrics=BLANK)
    too_old, _ = yt_post(days_ago=100)
    no_video_id, _ = yt_post(days_ago=1, with_id=False)
    failed_upload, _ = yt_post(days_ago=1, status=PostStatusEnum.error)
    mine = {fresh, checked_recently, checked_yesterday, never_checked, too_old, no_video_id, failed_upload}

    assert set(tasks.youtube_posts_due(session, now)) & mine == {fresh, checked_yesterday, never_checked}
    set_settings(yt_metrics_days=120)
    assert too_old in tasks.youtube_posts_due(session, now)
    set_settings(yt_metrics_days=0)
    assert set(tasks.youtube_posts_due(session, now)) & mine == {fresh}


def test_periodic_refresh_sends_youtube_as_one_batch(yt_post, make_post, session, monkeypatch):
    per_post, batches = [], []
    monkeypatch.setattr(tasks.refresh_metrics_for_post, "schedule", lambda **kw: per_post.append(kw["args"][0]))
    monkeypatch.setattr(tasks.refresh_youtube_metrics, "schedule", lambda **kw: batches.append(kw["args"][0]))
    yt_ids = {yt_post(days_ago=1)[0] for _ in range(3)}
    tg_id, _ = make_post("tg", status=PostStatusEnum.done)
    tg = session.get(Post, tg_id)
    tg.published_at = datetime.utcnow() - timedelta(days=1)
    session.add(tg)
    session.commit()

    tasks.refresh_recent_metrics.call_local()
    assert len(batches) == 1 and yt_ids <= set(batches[0])
    assert tg_id in per_post and not yt_ids & set(per_post)


def test_backfill_reaches_videos_past_the_window(yt_post, yt_api):
    post_id, vid = yt_post(days_ago=200, metrics=BLANK)
    yt_api.videos = {vid: _item(vid, views=900)}
    tasks.backfill_youtube_metrics.call_local()
    assert _metrics(post_id)["views"] == 900
    assert vid in [v for ids, _ in yt_api.calls for v in ids]
    assert all(len(ids) <= 50 for ids, _ in yt_api.calls)


# ── web ──────────────────────────────────────────────────────────────────────

def test_settings_save_accepts_yt_metrics_days(client):
    client.post("/api/settings/save", data={"yt_metrics_days": "45"})
    assert load_settings()["yt_metrics_days"] == 45
    client.post("/api/settings/save", data={"yt_metrics_days": "abc"})
    assert load_settings()["yt_metrics_days"] == 45
    client.post("/api/settings/save", data={"yt_metrics_days": "99999"})
    assert load_settings()["yt_metrics_days"] == 3650


def test_reconnecting_youtube_schedules_the_backfill(client, set_settings, monkeypatch):
    set_settings(yt_client_id="cid", yt_client_secret="csec", oauth_state_youtube="state-1")
    scheduled = []
    monkeypatch.setattr(tasks.backfill_youtube_metrics, "schedule", lambda **kw: scheduled.append(kw))
    _route_httpx(monkeypatch, lambda req: httpx.Response(200, json={
        "access_token": "at-new", "refresh_token": "rt-new", "expires_in": 3600}))
    r = client.get("/api/accounts/callback/youtube", params={"code": "c", "state": "state-1"})
    assert r.status_code == 200 and "YouTube connected" in r.text
    assert load_settings()["yt_refresh_token"] == "rt-new"
    assert len(scheduled) == 1


def test_update_metrics_button_batches_youtube(client, yt_post, yt_api):
    posts = [yt_post() for _ in range(3)]
    mine = {vid for _, vid in posts}
    yt_api.videos = {vid: _item(vid, views=5) for vid in mine}
    assert client.post("/api/analytics/refresh").status_code == 200
    # (the table's other YouTube posts from earlier tests ride along in the same calls)
    calls_with_mine = [set(ids) for ids, _ in yt_api.calls if set(ids) & mine]
    assert calls_with_mine == [calls_with_mine[0]] and mine <= calls_with_mine[0]
    assert all(len(ids) <= 50 for ids, _ in yt_api.calls)
    assert all(_metrics(pid)["views"] == 5 for pid, _ in posts)


def test_analytics_table_flags_metric_errors(client, yt_post):
    yt_post(metrics={"error": "yt_reauth_needed: yt_refresh_failed: invalid_grant"})
    r = client.get("/api/analytics/table")
    assert "Metrics fetch failed: yt_reauth_needed" in r.text
