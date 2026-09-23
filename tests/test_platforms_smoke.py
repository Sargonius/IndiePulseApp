"""Runs every publisher module through its first code path, so a broken import or an
undefined name left over from the per-platform split can't hide until a real publish."""
import os
import uuid

import pytest

from app.jobs import tasks
from app.models.models import Job, Post, PostStatusEnum
from app.platforms import METRICS, PUBLISHERS, platform_key
from app.models.models import PlatformEnum
from tests.conftest import reload


def test_every_platform_has_a_publisher():
    assert {p.value for p in PlatformEnum} == set(PUBLISHERS)
    assert platform_key(PlatformEnum.fb_ru) == "fb_ru"
    assert platform_key("PlatformEnum.x") == "x"
    assert set(METRICS) <= set(PUBLISHERS)


@pytest.mark.parametrize("platform, marker", [
    ("yt", "yt_no_media"),
    ("fb_ru", "fb_missing_page_or_token"),
    ("fb_en", "fb_missing_page_or_token"),
    ("reddit", "reddit_not_configured"),
    ("ig_reels", "ig_no_media"),
    ("tiktok", "tiktok_no_media"),
    ("tg", "tg_not_configured"),
    ("bluesky", "bluesky_not_configured"),
])
def test_unconfigured_publishers_fail_cleanly(make_post, platform, marker):
    post_id, job_id = make_post(platform)
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error
    assert marker in (post.error_text or "") + (reload(Job, job_id).last_error or "")


@pytest.fixture
def clip():
    name = f"_pytest_{uuid.uuid4().hex[:8]}.mp4"
    path = os.path.join("media", name)
    os.makedirs("media", exist_ok=True)
    with open(path, "wb") as f:
        f.write(b"\x00" * 1024)
    yield name
    os.remove(path)


@pytest.mark.parametrize("platform", ["ig_reels", "tiktok"])
def test_export_mode_without_token(make_post, set_settings, clip, tmp_path, platform):
    set_settings(tiktok_export_dir=str(tmp_path))
    post_id, job_id = make_post(platform, media_vertical_path=clip, title="Clip")
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert reload(Post, post_id).status == PostStatusEnum.awaiting_finish
    assert (tmp_path / clip).exists()


def test_metrics_dispatch_without_platform_id_is_empty(make_post):
    post_id, _ = make_post("x", status=PostStatusEnum.done)
    assert METRICS["x"](reload(Post, post_id)) == {}
