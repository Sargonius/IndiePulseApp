"""A post with media never goes out without it; oversized videos are compressed to fit."""
import os
import shutil
import subprocess

import pytest
import requests

from app.jobs import tasks
from app.models.models import Post, PostStatusEnum
from app.services import discord_client, video_editor, x_media
from app.services.discord_client import DiscordClient, DiscordSendError
from tests.conftest import reload
from tests.test_publish import FakeDiscord, FakeResp, fake_discord  # noqa: F401  (fixture)

_VENDOR = os.path.abspath(os.path.join("vendor", "ffmpeg", "ffmpeg.exe"))
FFMPEG = _VENDOR if os.path.exists(_VENDOR) else shutil.which("ffmpeg")


# --- Discord -------------------------------------------------------------------

def test_discord_client_refuses_oversized_attachment(tmp_path, set_settings, monkeypatch):
    set_settings(discord_upload_limit_mb=1)
    big = tmp_path / "clip.mp4"
    big.write_bytes(b"0" * (2 * 1024 * 1024))
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("nothing may be sent"))
    with pytest.raises(DiscordSendError) as err:
        DiscordClient("https://hook/x").send_message("hi", file_paths=[str(big)])
    assert err.value.delivered is False
    assert "over the 1 MB upload limit" in str(err.value)
    with pytest.raises(DiscordSendError, match="not found"):
        DiscordClient("https://hook/x").send_message("hi", file_paths=[str(tmp_path / "gone.png")])


class CaptureDiscord(FakeDiscord):
    files: list = []

    def send_message(self, content, file_path=None, file_paths=None):
        CaptureDiscord.files.append(file_paths)
        return super().send_message(content, file_path, file_paths)


def test_discord_video_is_fitted_once_for_both_webhooks(make_post, fake_discord, monkeypatch, tmp_path):
    fitted = str(tmp_path / "clip_9.5mb.mp4")
    fits = []
    monkeypatch.setattr(video_editor, "ensure_video_size_limit",
                        lambda path, platform, cfg: fits.append((path, platform)) or fitted)
    monkeypatch.setattr(discord_client, "DiscordClient", CaptureDiscord)
    CaptureDiscord.files = []
    post_id, job_id = make_post("discord", {"gallery": ["shot.png", "clip.mp4"]})
    fake_discord.plan = {"https://hook/ru": ["m1"], "https://hook/en": ["m2"]}
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert fits == [(os.path.abspath("media/clip.mp4"), "discord")]
    assert CaptureDiscord.files == [[fitted], [fitted]]  # the video goes alone


def test_discord_unfittable_video_fails_without_sending_or_retrying(make_post, fake_discord, monkeypatch):
    def too_large(path, platform, cfg):
        raise video_editor.VideoTooLarge(f"{video_editor.TOO_LARGE}: discord: video 80.0 MB (300 s) ...")
    monkeypatch.setattr(video_editor, "ensure_video_size_limit", too_large)
    post_id, job_id = make_post("discord", {"media_rel": "clip.mp4"})
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert fake_discord.calls == []
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error
    assert video_editor.TOO_LARGE in post.error_text

    scheduled = []
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda *a, **k: scheduled.append(k))
    tasks._maybe_retry_publish(job_id, post_id, "error")
    assert scheduled == []  # compression won't succeed on a rerun either


# --- X ------------------------------------------------------------------------

@pytest.fixture
def x_ready(monkeypatch, set_settings):
    """X connected; returns the list of tweets that were posted."""
    set_settings(x_api_key="k", x_api_secret="s", x_oauth_token="t", x_oauth_token_secret="ts")
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
            return FakeResp(201, {"data": {"id": str(100 + len(posted))}})

    import httpx
    import app.services.tokens as tokens
    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setattr(tokens, "x_ensure_token", lambda: "tok")
    return posted


def test_x_media_failure_posts_no_text_only_tweet(make_post, x_ready, monkeypatch):
    def reset(self, path):
        raise RuntimeError("APPEND 3 failed 3 times, last: ConnectionError")
    monkeypatch.setattr(x_media.XMediaUploader, "upload", reset)
    post_id, job_id = make_post("x", {"gallery": ["shot.png"], "media_rel": "shot.png"})
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert x_ready == []
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error
    assert post.platform_post_id is None  # so the auto-retry may run
    assert "x_media_upload_failed" in post.error_text


def test_x_without_oauth1_keys_refuses_media_post(make_post, x_ready, set_settings):
    set_settings(x_oauth_token="")
    post_id, job_id = make_post("x", {"media_rel": "shot.png"})
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert x_ready == []
    assert "OAuth1" in reload(Post, post_id).error_text


def test_x_tweets_with_uploaded_media(make_post, x_ready, monkeypatch):
    monkeypatch.setattr(x_media.XMediaUploader, "upload", lambda self, path: "m-" + os.path.basename(path))
    post_id, job_id = make_post("x", {"gallery": ["a.png", "b.png"]})
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert x_ready[0]["media"] == {"media_ids": ["m-a.png", "m-b.png"]}


def _fake_upload_api(calls, append_failures):
    def fake_request(method, url, auth=None, **kw):
        cmd = (kw.get("data") or kw.get("params") or {}).get("command")
        calls.append(cmd)
        if cmd == "APPEND" and calls.count("APPEND") <= append_failures:
            raise requests.ConnectionError("('Connection aborted.', ConnectionResetError(10054))")
        return {
            "INIT": FakeResp(202, {"media_id_string": "m1"}),
            "FINALIZE": FakeResp(201, {"media_id_string": "m1",
                                       "processing_info": {"state": "pending", "check_after_secs": 1}}),
            "STATUS": FakeResp(200, {"processing_info": {"state": "succeeded"}}),
        }.get(cmd, FakeResp(204, {}))
    return fake_request


@pytest.fixture
def uploader(monkeypatch):
    monkeypatch.setattr(x_media.time, "sleep", lambda s: None)
    monkeypatch.setattr(x_media.XMediaUploader, "_oauth1_headers", lambda self: None)
    return x_media.XMediaUploader("t", "s", "k", "c")


def test_x_upload_resends_a_dropped_chunk(uploader, tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"a" * 10)
    calls = []
    monkeypatch.setattr(x_media.requests, "request", _fake_upload_api(calls, append_failures=1))
    assert uploader.upload(str(clip)) == "m1"
    assert calls == ["INIT", "APPEND", "APPEND", "FINALIZE", "STATUS"]


def test_x_upload_gives_up_after_three_resets(uploader, tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"a" * 10)
    calls = []
    monkeypatch.setattr(x_media.requests, "request", _fake_upload_api(calls, append_failures=99))
    with pytest.raises(RuntimeError, match="APPEND 0 failed 3 times"):
        uploader.upload(str(clip))
    assert calls.count("APPEND") == 3


def test_x_upload_client_error_is_not_retried(uploader, tmp_path, monkeypatch):
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"a")
    calls = []

    def bad_request(method, url, auth=None, **kw):
        calls.append(1)
        r = FakeResp(400, {})
        r.text = '{"error": "InvalidMedia"}'
        return r
    monkeypatch.setattr(x_media.requests, "request", bad_request)
    with pytest.raises(RuntimeError, match="INIT HTTP 400"):
        uploader.upload(str(clip))
    assert len(calls) == 1


# --- Telegram / generic -----------------------------------------------------------

def test_telegram_retry_resends_the_whole_file(tmp_path, monkeypatch):
    from app.services import telegram_client
    f = tmp_path / "v.mp4"
    f.write_bytes(b"abcdef")
    seen = []

    def fake_post(url, data=None, files=None, timeout=None):
        seen.append(files["video"][1].read())
        if len(seen) == 1:
            raise requests.ConnectionError("reset")
        return FakeResp(200, {"ok": True})
    monkeypatch.setattr(telegram_client.time, "sleep", lambda s: None)
    with open(f, "rb") as fh:
        telegram_client._retry_request(fake_post, "u", files={"video": ("v.mp4", fh, "video/mp4")}, timeout=1)
    assert seen == [b"abcdef", b"abcdef"]


def test_crashing_publisher_marks_the_post_failed(make_post, monkeypatch):
    from app import platforms
    monkeypatch.setitem(platforms.PUBLISHERS, "tg", lambda session, post, job: 1 / 0)
    post_id, job_id = make_post("tg")
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error  # was left 'running' until the watchdog
    assert "ZeroDivisionError" in post.error_text


# --- compression (real ffmpeg) ------------------------------------------------------

@pytest.fixture
def big_clip(tmp_path, monkeypatch):
    if not FFMPEG:
        pytest.skip("needs ffmpeg")
    src = tmp_path / "big.mp4"
    subprocess.run([FFMPEG, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30:duration=6",
                    "-f", "lavfi", "-i", "sine=duration=6", "-c:v", "libx264", "-b:v", "8M",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(src)], check=True)
    monkeypatch.setattr(video_editor, "_ffmpeg_path", lambda: FFMPEG)
    monkeypatch.chdir(tmp_path)  # render/fit/ lands in tmp
    return str(src)


def test_oversized_video_is_compressed_under_the_limit(big_clip):
    assert os.path.getsize(big_clip) > 3 * video_editor.MB
    out = video_editor.ensure_video_size_limit(big_clip, "discord", {"discord_upload_limit_mb": 3})
    assert out != big_clip
    assert os.path.getsize(out) <= 3 * 0.95 * video_editor.MB
    assert video_editor._probe_video_info(out)["duration"] > 5
    # the RU and EN webhooks (and a retry) reuse the same fitted copy
    assert video_editor.ensure_video_size_limit(big_clip, "discord", {"discord_upload_limit_mb": 3}) == out
    assert os.listdir(os.path.dirname(out)) == [os.path.basename(out)]  # no temp leftovers


def test_video_too_long_for_the_limit_raises(big_clip):
    with pytest.raises(video_editor.VideoTooLarge, match=video_editor.TOO_LARGE):
        video_editor.ensure_video_size_limit(big_clip, "discord", {"discord_upload_limit_mb": 0.05})


# --- Facebook -------------------------------------------------------------------

@pytest.fixture
def fb_ready(set_settings, monkeypatch):
    """FB RU page configured; FacebookClient calls are recorded, nothing hits the network."""
    from app.services import facebook_client
    set_settings(fb_ru_page_id="page", fb_ru_page_access_token="tok")
    calls = []

    class FakeFB:
        def __init__(self, token):
            pass

        def create_post(self, page_id, message):
            calls.append(("post", message))
            return {"id": "page_1"}

        def create_photo(self, page_id, path, caption=""):
            calls.append(("photo", os.path.basename(path)))
            return {"post_id": "page_2"}

        def create_video(self, page_id, path, description=""):
            calls.append(("video", os.path.basename(path), description))
            return {"id": "777"}

        def video_post_id(self, page_id, video_id):
            return f"{page_id}_{video_id}"

        def get_permalink(self, post_id):
            return f"https://www.facebook.com/{post_id}"

    monkeypatch.setattr(facebook_client, "FacebookClient", FakeFB)
    monkeypatch.setattr(video_editor, "ensure_video_size_limit", lambda path, platform, cfg: path)
    return calls


def test_facebook_posts_the_video_itself(make_post, fb_ready):
    post_id, job_id = make_post("fb_ru", {"gallery": ["clip.mp4"], "media_rel": "clip.mp4"})
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert [c[0] for c in fb_ready] == ["video"]  # was a text-only /feed post
    assert fb_ready[0][2].startswith("Привет")
    assert reload(Post, post_id).platform_post_id == "page_777"


def test_facebook_unknown_media_type_is_not_posted_as_text(make_post, fb_ready):
    post_id, job_id = make_post("fb_ru", {"media_rel": "notes.pdf"})
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert fb_ready == []


def test_facebook_video_post_id_resolution(monkeypatch):
    from app.services import facebook_client
    answers = []

    class FakeHttp:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            return answers.pop(0)

    monkeypatch.setattr(facebook_client.httpx, "Client", FakeHttp)
    fb = facebook_client.FacebookClient("tok")
    answers.append(FakeResp(200, {"post_id": "555", "id": "777"}))
    assert fb.video_post_id("page", "777") == "page_555"
    answers.append(FakeResp(400, {"error": {"message": "nonexisting field (post_id)"}}))
    assert fb.video_post_id("page", "777") == "777"


# --- Bluesky --------------------------------------------------------------------

class FakeBskyHttp:
    """Routes BlueskyClient's httpx calls by XRPC method and records every request."""
    log: list = []
    routes: dict = {}

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def _answer(self, url, kw):
        method = url.rsplit("/", 1)[-1]
        FakeBskyHttp.log.append((method, kw))
        answer = FakeBskyHttp.routes[method]
        answer = answer.pop(0) if isinstance(answer, list) else answer
        r = FakeResp(*answer)
        r.text = str(answer[1])
        r.raise_for_status = lambda: None if r.status_code < 400 else pytest.fail(f"{method} failed")
        return r

    def get(self, url, **kw):
        return self._answer(url, kw)

    def post(self, url, **kw):
        return self._answer(url, kw)


@pytest.fixture
def bsky(monkeypatch, tmp_path):
    import time
    from app.services import bluesky_client
    monkeypatch.setattr(bluesky_client.httpx, "Client", FakeBskyHttp)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    # the aspect-ratio probe needs ffprobe; without vendor/ffmpeg (CI, a fresh checkout) it would try to
    # download ffmpeg through the faked client and show up in the request log
    from app.services import video_editor
    monkeypatch.setattr(video_editor, "_probe_video_info", lambda path: {})
    FakeBskyHttp.log = []
    blob = {"$type": "blob", "ref": {"$link": "bafyvideo"}, "mimeType": "video/mp4", "size": 3}
    FakeBskyHttp.routes = {
        "com.atproto.server.getServiceAuth": (200, {"token": "svc"}),
        "app.bsky.video.uploadVideo": (200, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_CREATED"}}),
        "app.bsky.video.getJobStatus": [
            (200, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_ENCODING", "progress": 40}}),
            (200, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_COMPLETED", "blob": blob}}),
        ],
        "com.atproto.repo.createRecord": (200, {"uri": "at://did:plc:me/app.bsky.feed.post/abc", "cid": "c1"}),
    }
    client = bluesky_client.BlueskyClient("me.bsky.social", "pw")
    client.access_jwt, client.did, client.handle = "jwt", "did:plc:me", "me.bsky.social"
    client.did_doc = {"service": [{"id": "#atproto_pds", "type": "AtprotoPersonalDataServer",
                                   "serviceEndpoint": "https://pds.example.host.bsky.network"}]}
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"vid")
    return client, str(clip), blob


def _methods():
    return [m for m, _ in FakeBskyHttp.log]


def test_bluesky_video_goes_through_the_video_service(bsky):
    client, clip, blob = bsky
    uri, url = client.post_thread("Hello", video_path=clip)
    assert _methods() == ["com.atproto.server.getServiceAuth", "app.bsky.video.uploadVideo",
                          "app.bsky.video.getJobStatus", "app.bsky.video.getJobStatus",
                          "com.atproto.repo.createRecord"]
    auth = FakeBskyHttp.log[0][1]["params"]
    assert auth["aud"] == "did:web:pds.example.host.bsky.network"
    assert auth["lxm"] == "com.atproto.repo.uploadBlob"
    upload = FakeBskyHttp.log[1][1]
    assert upload["params"] == {"did": "did:plc:me", "name": "clip.mp4"}
    assert upload["headers"]["Authorization"] == "Bearer svc"
    embed = FakeBskyHttp.log[-1][1]["json"]["record"]["embed"]
    assert embed["$type"] == "app.bsky.embed.video" and embed["video"] == blob
    assert url == "https://bsky.app/profile/me.bsky.social/post/abc"


def test_bluesky_already_uploaded_video_reuses_its_blob(bsky):
    client, clip, blob = bsky
    FakeBskyHttp.routes["app.bsky.video.uploadVideo"] = (
        409, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_COMPLETED", "error": "already_exists", "blob": blob}})
    client.post("Hello", video_path=clip)
    assert "app.bsky.video.getJobStatus" not in _methods()


def test_bluesky_failed_video_is_not_posted_as_text(bsky):
    client, clip, _ = bsky
    FakeBskyHttp.routes["app.bsky.video.getJobStatus"] = (
        200, {"jobStatus": {"jobId": "j1", "state": "JOB_STATE_FAILED", "error": "video too long"}})
    with pytest.raises(RuntimeError, match="video too long"):
        client.post("Hello", video_path=clip)
    assert "com.atproto.repo.createRecord" not in _methods()


def test_bluesky_failed_image_is_not_posted_as_text(bsky, tmp_path):
    client, _, _ = bsky
    FakeBskyHttp.routes["com.atproto.repo.uploadBlob"] = (500, {"error": "InternalServerError"})
    img = tmp_path / "shot.png"
    from PIL import Image
    Image.new("RGB", (8, 8)).save(img)
    with pytest.raises(RuntimeError, match="image upload HTTP 500"):
        client.post("Hello", image_paths=[str(img)])
    assert "com.atproto.repo.createRecord" not in _methods()


def test_bluesky_publisher_hands_the_video_over(make_post, set_settings, monkeypatch):
    from app.services import bluesky_client
    set_settings(bluesky_handle="me.bsky.social", bluesky_app_password="pw")
    seen = {}

    def fake_thread(self, text, hashtags="", image_paths=None, langs=None, video_path=None):
        seen.update(image_paths=image_paths, video_path=video_path)
        return "at://x/app.bsky.feed.post/1", "https://bsky.app/profile/me/post/1"
    monkeypatch.setattr(bluesky_client.BlueskyClient, "post_thread", fake_thread)
    monkeypatch.setattr(video_editor, "ensure_video_size_limit", lambda path, platform, cfg: path + ".fit")
    post_id, job_id = make_post("bluesky", {"gallery": ["shot.png", "clip.mp4"]})
    assert tasks._publish_news_post_impl(job_id, post_id) == "ok"
    assert seen == {"image_paths": [], "video_path": os.path.abspath("media/clip.mp4") + ".fit"}


# --- VK ----------------------------------------------------------------------------

def test_vk_failed_video_does_not_post_the_rest(make_post, set_settings, monkeypatch):
    from app.services import vk_client
    set_settings(vk_access_token="tok", vk_group_id="123")
    posted = []

    class FakeVK:
        def __init__(self, token):
            pass

        def upload_photo_to_wall(self, group_id, path, token_override=None):
            return "photo-123_1"

        def get_video_upload_server(self, group_id, name, token_override=None):
            raise RuntimeError("video.save: access denied")

        def wall_post(self, owner_id, message):
            posted.append(message)
            return {"post_id": 1}

    monkeypatch.setattr(vk_client, "VKClient", FakeVK)
    import httpx
    monkeypatch.setattr(httpx, "Client", lambda *a, **k: pytest.fail("wall.post must not be called"))
    post_id, job_id = make_post("vk", {"gallery": ["shot.png", "clip.mp4"]})
    assert tasks._publish_news_post_impl(job_id, post_id) == "error"
    assert posted == []
    assert "access denied" in reload(Post, post_id).error_text
