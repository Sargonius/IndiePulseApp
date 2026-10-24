"""X API is prepaid: 402 'credits depleted' is remembered, shown and never auto-retried."""
from datetime import datetime

import httpx
import pytest

from app.jobs import tasks
from app.models.models import Job, Post, PostStatusEnum
from app.platforms.x import thread_parts
from app.services import x_billing
from app.services.settings import load_settings
from tests.conftest import reload

DEPLETED_BODY = {"detail": "credits depleted", "status": 402, "title": "Payment Required",
                 "type": "https://api.x.com/2/problems/credits-depleted"}
_REAL_CLIENT = httpx.Client


def _route_httpx(monkeypatch, handler):
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **kw: _REAL_CLIENT(*a, transport=httpx.MockTransport(handler), **kw))


@pytest.fixture
def x_token(monkeypatch):
    import app.services.tokens as tokens
    monkeypatch.setattr(tokens, "x_ensure_token", lambda *a, **kw: "tok")


def test_remember_sets_once_clears_on_success_and_ignores_other_errors(monkeypatch):
    saves = []
    real_save = x_billing.save_settings
    monkeypatch.setattr(x_billing, "save_settings", lambda p: (saves.append(dict(p)), real_save(p))[1])

    assert x_billing.remember(402, DEPLETED_BODY) is True
    since = load_settings()["x_credits_error_at"]
    assert since > 0
    assert x_billing.remember(402, DEPLETED_BODY) is True  # every 30-min metrics run hits this
    assert x_billing.remember(500, {"title": "boom"}) is False
    assert load_settings()["x_credits_error_at"] == since
    assert len(saves) == 1

    # users/me is served even on an empty balance: its success clears nothing
    assert x_billing.remember(200, {"data": {}}, billed=False) is False
    assert load_settings()["x_credits_error_at"] == since
    assert x_billing.remember(200, {"data": {}}) is False
    assert load_settings()["x_credits_error_at"] == 0
    assert x_billing.remember(201, {"data": {}}) is False
    assert len(saves) == 2


def test_402_fails_the_post_with_a_clear_error_and_skips_auto_retry(make_post, monkeypatch, x_token):
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(402, json=DEPLETED_BODY)

    _route_httpx(monkeypatch, handler)
    post_id, job_id = make_post("x", text_en="Patch is live")
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error
    assert x_billing.DEPLETED in post.error_text
    assert load_settings()["x_credits_error_at"] > 0

    tasks._maybe_retry_publish(job_id, post_id, "error")
    assert reload(Post, post_id).status == PostStatusEnum.error  # not requeued
    assert "auto-retry" not in (reload(Job, job_id).last_error or "")
    assert len(calls) == 1


def test_banner_counts_failed_posts_and_the_check_clears_it(client, make_post, set_settings, monkeypatch, x_token):
    assert client.get("/api/x/alert").text == ""
    from sqlmodel import Session
    from app.db.session import engine
    with Session(engine) as s:
        failed = x_billing.failed_posts(s) + 1  # the DB is shared across tests
    make_post("x", status=PostStatusEnum.error)
    pid, _ = make_post("x", status=PostStatusEnum.error)
    live, _ = make_post("x", status=PostStatusEnum.done)
    with Session(engine) as s:
        p = s.get(Post, pid)
        p.error_text = x_billing.ERROR_TEXT
        t = s.get(Post, live)
        t.platform_post_id, t.published_at = "777", datetime.utcnow()
        s.add(p)
        s.add(t)
        s.commit()
    set_settings(x_credits_error_at=1_790_000_000)

    html = client.get("/api/x/alert").text
    assert "out of credits" in html and "console.x.com" in html
    assert f"Posts not sent: <b>{failed}</b>" in html

    seen = []

    def x_api(req):  # users/me works on an empty balance, tweet reads don't
        seen.append(req.url.path + "?" + req.url.query.decode())
        if req.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"username": "dev"}})
        return httpx.Response(402, json=DEPLETED_BODY)

    _route_httpx(monkeypatch, x_api)
    still = client.post("/api/x/credits_check").text
    assert seen == ["/2/tweets?ids=777"]  # the probe reads our latest tweet (billed)
    assert "still answers 402" in still
    assert load_settings()["x_credits_error_at"] > 0

    _route_httpx(monkeypatch, lambda req: httpx.Response(200, json={"data": {"username": "dev"}}))
    ok = client.post("/api/x/credits_check").text
    assert "accepts requests again" in ok and "Retry" in ok
    assert load_settings()["x_credits_error_at"] == 0
    assert client.get("/api/x/alert").text == ""


def test_usage_prices_links_and_threads(session, make_post):
    def done(post_id, **job_error):
        post = session.get(Post, post_id)
        post.status, post.published_at = PostStatusEnum.done, datetime.utcnow()
        session.add(post)
        if job_error:
            job = tasks.find_publish_jobs(session, post_id)[0]
            job.last_error = job_error["last_error"]
            session.add(job)
        session.commit()

    before = x_billing.usage(session)
    done(make_post("x", text_en="Short one")[0])
    done(make_post("x", text_en="Wishlist: https://store.steampowered.com/app/1")[0])
    long_text = " ".join("Sentence number %d is here." % i for i in range(30))
    done(make_post("x", text_en=long_text)[0], last_error="x_280_limit_fallback_thread")
    done(make_post("x", text_en=long_text)[0])  # went out whole: X Premium
    done(make_post("tg", text_en="https://t.me/x")[0])  # other platforms don't count

    u = x_billing.usage(session)
    thread = len(thread_parts(long_text, ""))
    assert thread > 1
    assert u["posts"] - before["posts"] == 4
    assert u["tweets"] - before["tweets"] == 3 + thread
    assert u["with_url"] - before["with_url"] == 1
    assert u["tweet_cost"] - before["tweet_cost"] == pytest.approx(
        (2 + thread) * x_billing.PRICE_POST + x_billing.PRICE_POST_URL)


def test_billing_guide_and_status_badge(client, set_settings):
    html = client.get("/api/x/billing").text
    assert "X API billing" in html and "X isn’t connected" in html
    set_settings(x_access_token="tok", x_credits_error_at=1_790_000_000)
    html = client.get("/api/x/billing").text
    assert "X API credits ran out" in html and "Auto-recharge" in html
    assert "Out of X API credits" in client.get("/api/settings/status").json()["x"]["message"]
    assert "x_billing" in client.get("/accounts").text


def test_health_check_names_the_empty_balance(set_settings, monkeypatch, x_token):
    from app.services import health_check
    set_settings(x_access_token="tok")
    monkeypatch.setattr(x_billing, "_latest_tweet_id", lambda: "777")

    def x_api(req):
        if req.url.path.endswith("/users/me"):
            return httpx.Response(200, json={"data": {"username": "dev"}})
        return httpx.Response(402, json=DEPLETED_BODY)

    _route_httpx(monkeypatch, x_api)
    r = health_check._Result("x", "X")
    health_check.check_x(load_settings(), r)
    assert (r.status, r.summary) == ("error", "out of credits")
    assert "console.x.com" in r.hint
    assert load_settings()["x_credits_error_at"] > 0
