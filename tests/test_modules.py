"""🧩 Modules: someone without a VK group turns VK off and never sees it again — nor does the worker publish there."""
from sqlmodel import Session

from app import platforms
from app.core import modules
from app.db.session import engine
from app.jobs import tasks
from app.models.models import Post, PostStatusEnum
from app.services import marketing_collect
from tests.conftest import reload


def test_a_module_needing_one_that_is_off_is_off_too():
    assert modules.resolve_off([]) == frozenset()
    assert modules.resolve_off(["tiktok"]) == {"tiktok", "tiktok_pipeline"}
    assert modules.resolve_off(["shorts", "no-such-module"]) == {"shorts", "tiktok_pipeline"}


def test_pages_and_api_of_a_module_that_is_off_answer_404(client, set_settings):
    assert client.get("/outreach").status_code == 200
    set_settings(modules_disabled=["outreach", "vk"])
    page = client.get("/outreach")
    assert page.status_code == 404 and "Creator outreach is turned off" in page.text
    api = client.get("/api/outreach/contacts")
    assert api.status_code == 404 and api.json()["detail"] == "module_off"
    assert client.get("/api/accounts/connect/vk").status_code == 404  # its login too
    assert client.get("/api/accounts/connect/x").status_code != 404   # not someone else's
    assert client.get("/outreachers").status_code == 404 and "turned off" not in client.get("/outreachers").text


def test_what_is_off_disappears_from_the_pages(client, set_settings):
    home = client.get("/").text
    assert 'href="/outreach"' in home and "IP_OFF_PLATFORMS = []" in home
    set_settings(modules_disabled=["outreach", "facebook"])
    home = client.get("/").text
    assert 'href="/outreach"' not in home
    assert 'IP_OFF_PLATFORMS = ["fb_en", "fb_ru"]' in home  # their checkboxes on every page
    settings_page = client.get("/accounts").text
    assert "#section_facebook { display: none !important; }" in settings_page


def test_the_modules_page_turns_them_off_and_on(client):
    from app.services.settings import load_settings
    page = client.get("/modules")
    assert page.status_code == 200 and "Bluesky" in page.text and "TikTok pipeline" in page.text
    r = client.post("/api/modules/set", data={"module": "tiktok"})  # unchecked: nothing but the module
    assert r.status_code == 200 and "Off while TikTok is off" in r.text
    assert load_settings()["modules_disabled"] == ["tiktok"]
    client.post("/api/modules/set", data={"module": "tiktok", "on": "true"})
    assert load_settings()["modules_disabled"] == []
    assert client.post("/api/modules/set", data={"module": "nope"}).status_code == 404


def test_nothing_goes_to_a_platform_that_is_off(make_post, set_settings, monkeypatch):
    sent = []
    monkeypatch.setitem(platforms.PUBLISHERS, "tg", lambda session, post, job: sent.append(post.id))
    post_id, job_id = make_post("tg")
    set_settings(modules_disabled=["telegram"])
    assert tasks._publish_news_post_impl(job_id, post_id) == "module_off" and sent == []
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error and tasks.MODULE_OFF in post.error_text
    tasks._maybe_retry_publish(job_id, post_id, "module_off")  # waits for the user, no auto-retry
    assert reload(Post, post_id).status == PostStatusEnum.error


def test_the_worker_skips_jobs_of_modules_that_are_off(set_settings, monkeypatch):
    set_settings(modules_disabled=["outreach", "tiktok"])
    assert tasks.refresh_outreach_creators.call_local() == "module_off"
    assert tasks.tiktok_pipeline_tick.call_local() == "module_off"
    cfg = {"bluesky_handle": "dev.bsky.social", "tiktok_refresh_token": "t", "tiktok_scope": "user.info.stats"}

    def channels():
        with Session(engine) as session:
            return [k for k, _fn in marketing_collect._audience_jobs(session, cfg, force=True)]
    assert "bluesky" in channels() and "tiktok" not in channels()  # connected, but turned off
    set_settings(modules_disabled=[])
    assert "tiktok" in channels()
