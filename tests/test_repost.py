"""Reposting a published short: per-post texts, page/API, translation."""
import json
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import select

from app.models.models import Brief, Job, Post, PostStatusEnum
from app.platforms.common import brief_for_publish
from app.services import repost

CONFIGURED = {
    "x_access_token": "t", "x_api_key": "k", "x_api_secret": "s", "x_oauth_token": "o", "x_oauth_token_secret": "os",
    "tg_bot_token": "b", "tg_chat_id_ru": "c",
    "vk_access_token": "v", "vk_group_id": "1", "vk_user_access_token": "u",
    "discord_webhook_ru": "https://discord.test/ru", "discord_webhook_en": "https://discord.test/en",
}


@pytest.fixture
def short(session):
    """A rendered short already live on YouTube and VK."""
    def _make(title="Neon city drive", text_en="Night ride through the city {steam}", tags="#Shorts #indiegame",
              rendered=True, live=("yt", "vk_video"), **kw):
        b = Brief(user_id=1, type="video", title=title, text_en=text_en, tags_json=tags, vertical_916=True,
                  media_path="short_abc_clip.mp4",
                  media_vertical_path="short_abc_clip_vertical.mp4" if rendered else None, **kw)
        session.add(b)
        session.commit()
        for plat in live:
            session.add(Post(user_id=1, brief_id=b.id, platform=plat, status=PostStatusEnum.done,
                             platform_url=f"https://{plat}.test/1"))
        session.commit()
        session.refresh(b)
        return b
    return _make


@pytest.fixture
def scheduled(monkeypatch):
    from app.jobs import tasks
    calls = []
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda args, delay=0: calls.append(args))
    return calls


# ── brief_for_publish ────────────────────────────────────────────────────────

def test_post_overrides_replace_brief_texts_without_touching_it(session, short):
    b = short()
    post = Post(user_id=1, brief_id=b.id, platform="vk_video",
                overrides_json=json.dumps({"text_ru": "Ночная поездка", "text_en": "", "tags_json": "#инди"}))
    out = brief_for_publish(session, post)
    assert (out.text_ru, out.text_en, out.tags_json, out.title) == ("Ночная поездка", "", "#инди", "Neon city drive")
    session.expire_all()
    stored = session.get(Brief, b.id)
    assert stored.text_en == "Night ride through the city {steam}" and stored.tags_json == "#Shorts #indiegame"


def test_override_text_gets_the_platform_utm_link(session, short, project):
    p = project(steam_app_id=4242)
    b = short(project_id=p.id)
    post = Post(user_id=1, brief_id=b.id, platform="tg",
                overrides_json=json.dumps({"text_ru": "Играй {steam}", "text_en": ""}))
    text = brief_for_publish(session, post).text_ru
    q = parse_qs(urlsplit(text.split()[-1]).query)
    assert q["utm_source"] == ["telegram"] and q["utm_medium"] == ["shorts"]


def test_unknown_or_broken_overrides_are_ignored(session, short):
    b = short(text_en="plain")
    for raw in ("not json", "[1, 2]", json.dumps({"media_path": "x.mp4"})):
        assert brief_for_publish(session, Post(user_id=1, brief_id=b.id, platform="x", overrides_json=raw)) \
            .media_path == "short_abc_clip.mp4"
    assert brief_for_publish(session, Post(user_id=1, brief_id=b.id, platform="x")) is session.get(Brief, b.id)


# ── build_overrides ──────────────────────────────────────────────────────────

def test_overrides_put_the_caption_in_its_language_field():
    assert repost.build_overrides("vk_video", {"lang": "ru", "text": "Привет", "tags": "#a"}, CONFIGURED) == \
        {"text_ru": "Привет", "text_en": "", "tags_json": "#a"}
    assert repost.build_overrides("x", {"lang": "ja", "text": "夜のドライブ", "tags": "#b", "title": "T"}, CONFIGURED) == \
        {"text_en": "夜のドライブ", "text_ru": "", "tags_json": "#b", "title": "T"}


def test_overrides_drop_tags_where_the_publisher_ignores_them():
    assert repost.build_overrides("tg", {"lang": "ru", "text": "Привет", "tags": "#a"}, CONFIGURED)["tags_json"] == ""


def test_reddit_needs_only_a_title():
    assert repost.build_overrides("reddit", {"title": "  My game  ", "text": "ignored"}, CONFIGURED) == \
        {"title": "My game", "text_en": "", "text_ru": "", "tags_json": ""}
    assert repost.build_overrides("reddit", {"title": " "}, CONFIGURED) == {}


def test_discord_gets_one_text_per_webhook():
    item = {"text_ru": "Привет", "text_en": "Hello"}
    assert repost.build_overrides("discord", item, CONFIGURED) == {"text_ru": "Привет", "text_en": "Hello", "tags_json": ""}
    only_ru = {"discord_webhook_ru": "https://discord.test/ru"}
    assert repost.build_overrides("discord", item, only_ru)["text_en"] == ""
    default = {"discord_webhook_url": "https://discord.test/any"}
    assert repost.discord_langs(default) == ["default"]
    assert repost.build_overrides("discord", {"text": "Hi"}, default) == {"text_en": "Hi", "text_ru": "", "tags_json": ""}


def test_empty_caption_means_nothing_to_post():
    assert repost.build_overrides("x", {"lang": "en", "text": " ", "tags": ""}, CONFIGURED) == {}


def test_configured():
    assert repost.configured("x", CONFIGURED) and repost.configured("tg", CONFIGURED)
    assert not repost.configured("bluesky", CONFIGURED)
    assert not repost.configured("x", {k: v for k, v in CONFIGURED.items() if k != "x_oauth_token"})
    assert not repost.configured("discord", {})


# ── create_reposts ───────────────────────────────────────────────────────────

def test_create_reposts_queues_new_networks_and_skips_the_rest(session, short, scheduled):
    b = short()
    items = [
        {"platform": "x", "lang": "en", "text": "Night ride", "tags": "#indiegame"},
        {"platform": "tg", "lang": "ru", "text": "Ночная поездка"},
        {"platform": "vk_video", "lang": "ru", "text": "Ночная поездка"},   # already live
        {"platform": "bluesky", "lang": "en", "text": "Night ride"},        # not configured
        {"platform": "x", "lang": "en", "text": "again"},                  # twice
        {"platform": "nope"},
    ]
    res = repost.create_reposts(session, b, items, cfg=CONFIGURED)
    assert [c["platform"] for c in res["created"]] == ["x", "tg"]
    assert {(s["platform"], s["reason"]) for s in res["skipped"]} == {
        ("vk_video", "already"), ("bluesky", "not_configured"), ("x", "twice"), ("nope", "unknown_platform")}
    assert len(scheduled) == 2
    for job_id, post_id in scheduled:
        job = session.get(Job, job_id)
        payload = json.loads(job.payload_json)
        assert payload["post_id"] == post_id and payload["media_rel"] == "short_abc_clip_vertical.mp4"
        assert "allow_duplicate" not in payload
    tg = session.exec(select(Post).where(Post.brief_id == b.id, Post.platform == "tg")).one()
    assert json.loads(tg.overrides_json) == {"text_ru": "Ночная поездка", "text_en": "", "tags_json": ""}
    assert tg.status == PostStatusEnum.queued and tg.scheduled_at is None


def test_allow_duplicate_reposts_where_it_is_already_live(session, short, scheduled):
    b = short()
    res = repost.create_reposts(session, b, [{"platform": "vk_video", "lang": "ru", "text": "Снова"}],
                                allow_duplicate=True, cfg=CONFIGURED)
    assert [c["platform"] for c in res["created"]] == ["vk_video"]
    assert json.loads(session.get(Job, scheduled[0][0]).payload_json)["allow_duplicate"] is True


def test_failed_post_does_not_block_a_new_try(session, short, scheduled):
    b = short(live=())
    session.add(Post(user_id=1, brief_id=b.id, platform="tg", status=PostStatusEnum.error))
    session.commit()
    res = repost.create_reposts(session, b, [{"platform": "tg", "lang": "ru", "text": "Ещё раз"}], cfg=CONFIGURED)
    assert [c["platform"] for c in res["created"]] == ["tg"]


def test_unrendered_short_cannot_be_reposted(session, short, scheduled):
    with pytest.raises(ValueError):
        repost.create_reposts(session, short(rendered=False), [{"platform": "tg", "text": "x"}], cfg=CONFIGURED)


def test_page_context(session, short, set_settings):
    from app.services.settings import load_settings
    set_settings(**CONFIGURED)
    b = short(title="夜のドライブ", text_en="ネオンの街を走る")
    ctx = repost.page_context(session, b, load_settings())
    assert ctx["source"]["lang"] == "ja" and ctx["yt_url"] == "https://yt.test/1"
    assert ctx["video_url"] == "/media/short_abc_clip_vertical.mp4"
    by = {p["key"]: p for p in ctx["platforms"]}
    assert by["vk_video"]["existing"]["state"] == "live" and by["x"]["existing"] is None
    assert by["x"]["configured"] and not by["bluesky"]["configured"]
    assert by["discord"]["dual_langs"] == ["ru", "en"]


# ── translation ──────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, status, content="", text=""):
        self.status_code, self._content, self.text = status, content, text

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}


def test_translate_with_openai(monkeypatch, set_settings):
    from app.services import openai_client
    seen = {}

    def fake_chat(messages, **kw):
        seen["system"] = messages[0]["content"]
        seen["user"] = json.loads(messages[1]["content"])
        answer = '```json\n{"title": "夜のドライブ", "desc": "ネオンの街 {steam}", "tags": "#インディーゲーム, ゲーム開発"}\n```'
        return _Resp(200, answer), "gpt-test"

    set_settings(openai_api_key="key", openai_model="gpt-test")
    monkeypatch.setattr(openai_client, "chat_completion", fake_chat)
    out = repost.translate({"title": "Night drive", "desc": "Neon city {steam}", "tags": "#indiegame #gamedev"}, "ja")
    assert out == {"title": "夜のドライブ", "desc": "ネオンの街 {steam}", "tags": "#インディーゲーム #ゲーム開発",
                   "provider": "openai:gpt-test"}
    assert "Japanese" in seen["system"] and "{steam}" in seen["system"]
    assert seen["user"]["title"] == "Night drive"


def test_translate_keeps_empty_fields_empty(monkeypatch, set_settings):
    from app.services import openai_client
    set_settings(openai_api_key="key")
    monkeypatch.setattr(openai_client, "chat_completion",
                        lambda *a, **kw: (_Resp(200, '{"title": "Привет", "desc": "лишнее", "tags": "#x"}'), "m"))
    out = repost.translate({"title": "Hi", "desc": "", "tags": ""}, "ru")
    assert (out["title"], out["desc"], out["tags"]) == ("Привет", "", "")
    assert repost.translate({"title": "", "desc": " "}, "ru")["provider"] == ""


def test_translate_falls_back_to_google(monkeypatch):
    import deep_translator

    class FakeGoogle:
        def __init__(self, source, target):
            self.target = target

        def translate(self, text):
            return f"[{self.target}] {text}"

    monkeypatch.setattr(deep_translator, "GoogleTranslator", FakeGoogle)  # no AI key in the settings
    out = repost.translate({"title": "Hi", "desc": "Text", "tags": "#indiegame"}, "ru")
    assert out == {"title": "[ru] Hi", "desc": "[ru] Text", "tags": "#indiegame", "provider": "google"}


def test_translate_error_when_nothing_works(monkeypatch, set_settings):
    import deep_translator
    from app.services import openai_client

    class Broken:
        def __init__(self, *a, **kw):
            raise RuntimeError("offline")

    set_settings(openai_api_key="key")
    monkeypatch.setattr(openai_client, "chat_completion", lambda *a, **kw: (_Resp(500, text="boom"), "m"))
    monkeypatch.setattr(deep_translator, "GoogleTranslator", Broken)
    with pytest.raises(repost.TranslateError):
        repost.translate({"title": "Hi"}, "ja")


@pytest.mark.parametrize("raw, expected", [
    ("#a, b  #c", "#a #b #c"),
    ("#A #a ##b", "#A #b"),
    ("#インディーゲーム、#ゲーム開発", "#インディーゲーム #ゲーム開発"),
    ("", ""),
])
def test_normalize_tags(raw, expected):
    assert repost.normalize_tags(raw) == expected


def test_detect_lang():
    assert repost.detect_lang("夜のドライブ") == "ja"
    assert repost.detect_lang("Ночная поездка") == "ru"
    assert repost.detect_lang("Night ride") == "en"
    assert repost.detect_lang("123 !!") is None


# ── HTTP ─────────────────────────────────────────────────────────────────────

def test_translate_endpoint(client, monkeypatch):
    monkeypatch.setattr(repost, "translate", lambda data, target: {"title": f"{target}:{data['title']}"})
    assert client.post("/api/shorts/translate", json={"title": "Hi", "target": "not a language"}).status_code == 400
    assert client.post("/api/shorts/translate", json={"title": "Hi", "target": "ja"}).json() == {"title": "ja:Hi"}

    def fail(data, target):
        raise repost.TranslateError("translation failed")
    monkeypatch.setattr(repost, "translate", fail)
    r = client.post("/api/shorts/translate", json={"title": "Hi", "target": "ru"})
    assert r.status_code == 502 and "translation failed" in r.json()["detail"]


def test_repost_endpoint_and_pages(client, session, short, scheduled, set_settings):
    set_settings(**CONFIGURED)
    b = short()
    page = client.get(f"/shorts/repost/{b.id}")
    assert page.status_code == 200 and "Cross-post Short" in page.text and "Facebook RU" in page.text
    assert "Cross-post" in client.get("/shorts/board/partial").text

    r = client.post(f"/api/shorts/{b.id}/repost", json={"items": [{"platform": "tg", "lang": "ru", "text": "Привет"}]})
    assert r.status_code == 200 and [c["platform"] for c in r.json()["created"]] == ["tg"]

    assert client.post("/api/shorts/999999/repost", json={"items": []}).status_code == 404
    raw = short(rendered=False)
    assert client.post(f"/api/shorts/{raw.id}/repost", json={"items": []}).status_code == 400
    assert client.get("/shorts/repost/999999", follow_redirects=False).status_code == 303
