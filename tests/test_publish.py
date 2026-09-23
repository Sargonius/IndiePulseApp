import json

import pytest

from app.jobs import tasks
from app.models.models import Job, Post, PostStatusEnum
from app.services import discord_client
from app.services.discord_client import DiscordSendError
from tests.conftest import reload


def test_vk_video_goes_through_vk_branch(make_post):
    # no VK credentials -> the VK branch reports it; before the fix this post was
    # silently marked "done" without publishing anything
    post_id, job_id = make_post("vk_video", {"media_rel": "clip.mp4"})
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert reload(Post, post_id).status == PostStatusEnum.error
    assert "vk_not_configured" in reload(Job, job_id).last_error


def test_running_post_is_not_published_again(make_post):
    post_id, job_id = make_post("tg", status=PostStatusEnum.running)
    assert tasks._publish_news_post_impl(job_id, post_id) == "skipped_already_handled"
    assert reload(Post, post_id).status == PostStatusEnum.running


class FakeDiscord:
    """Stands in for DiscordClient; `plan` maps webhook url -> list of outcomes."""
    calls: list = []
    plan: dict = {}

    def __init__(self, url):
        self.url = url
        self.last_channel_id = None
        self.last_message_id = None

    def send_message(self, content, file_path=None, file_paths=None):
        FakeDiscord.calls.append(self.url)
        outcome = FakeDiscord.plan[self.url].pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        self.last_channel_id, self.last_message_id = "c" + self.url[-1], outcome
        return outcome, None


@pytest.fixture
def fake_discord(monkeypatch, set_settings):
    set_settings(discord_webhook_ru="https://hook/ru", discord_webhook_en="https://hook/en")
    FakeDiscord.calls = []
    FakeDiscord.plan = {}
    monkeypatch.setattr(tasks, "DiscordClient", FakeDiscord, raising=False)
    monkeypatch.setattr(discord_client, "DiscordClient", FakeDiscord)
    return FakeDiscord


def _requeue(post_id):
    from sqlmodel import Session
    from app.db.session import engine
    with Session(engine) as s:
        p = s.get(Post, post_id)
        p.status = PostStatusEnum.queued
        s.add(p)
        s.commit()


def test_discord_retry_only_resends_failed_webhook(make_post, fake_discord):
    post_id, job_id = make_post("discord")
    fake_discord.plan = {
        "https://hook/ru": ["m1"],
        "https://hook/en": [DiscordSendError("HTTP 500", delivered=False), "m2"],
    }
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    payload = json.loads(reload(Job, job_id).payload_json)
    assert payload["discord_sent"] == {"ru": "cu/m1"}

    _requeue(post_id)
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert fake_discord.calls == ["https://hook/ru", "https://hook/en", "https://hook/en"]
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.done
    assert post.platform_post_id == "cu/m1,cn/m2"


def test_discord_timeout_is_never_resent(make_post, fake_discord):
    post_id, job_id = make_post("discord")
    fake_discord.plan = {
        "https://hook/ru": [DiscordSendError("ReadTimeout", delivered=None)],
        "https://hook/en": ["m2"],
    }
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert "may have been posted" in reload(Post, post_id).error_text
    _requeue(post_id)
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert fake_discord.calls.count("https://hook/ru") == 1


def test_discord_rejected_attachment_falls_back_to_text(make_post, fake_discord, tmp_path, monkeypatch):
    img = tmp_path / "shot.png"
    img.write_bytes(b"x")
    post_id, job_id = make_post("discord", {"gallery": [str(img)]})
    fake_discord.plan = {
        "https://hook/ru": [DiscordSendError("HTTP 413", delivered=False), "m1"],
        "https://hook/en": ["m2"],
    }
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert fake_discord.calls.count("https://hook/ru") == 2


class FakeResp:
    def __init__(self, status, data):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data


def test_x_keeps_first_tweet_when_thread_reply_fails(make_post, monkeypatch):
    posted = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posted.append(json)
            if "reply" in json:
                return FakeResp(500, {"title": "boom"})
            return FakeResp(201, {"data": {"id": "111"}})

    import httpx
    import app.routers.api as api
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(api, "_x_ensure_token", lambda: "tok")
    long_text = ". ".join(["Sentence number %d is here" % i for i in range(30)]) + "."
    post_id, job_id = make_post("x", {"x_thread": True}, text_en=long_text)

    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.done
    assert post.platform_post_id == "111"
    assert "x_thread_partial" in reload(Job, job_id).last_error
    assert len(posted) == 2  # first tweet + the failed reply, no re-post


def test_x_long_text_falls_back_to_thread_on_403(make_post, monkeypatch):
    posted = []

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json=None, headers=None):
            posted.append(json)
            if len(json["text"]) > 280:
                return FakeResp(403, {"detail": "too long"})
            return FakeResp(201, {"data": {"id": str(len(posted))}})

    import httpx
    import app.routers.api as api
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(api, "_x_ensure_token", lambda: "tok")
    long_text = " ".join(["Word%d." % i for i in range(120)])
    post_id, job_id = make_post("x", text_en=long_text)
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert reload(Post, post_id).platform_post_id == "2"  # first tweet of the thread
    assert all(len(p["text"]) <= 280 for p in posted[1:])
    assert "reply" in posted[-1]
