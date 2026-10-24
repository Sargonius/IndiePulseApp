"""Languages & destinations beyond the post forms: the Shorts factory, its cross-post page, the TikTok pipeline,
the post-edit API, the health check, follower counts, the old /news page and a fresh install."""
import json
import os

import pytest
from sqlmodel import select

from app.models.models import Brief, Job, JobKindEnum, Post
from app.platforms.common import brief_for_publish, post_text_lang
from app.services import destinations, languages
from app.services.settings import load_settings
from tests.conftest import reload
from tests.test_destinations import OWNER, _publish, _with_ja, networks  # noqa: F401  (networks: a fixture)


@pytest.fixture
def scheduled(monkeypatch):
    """Publish and render jobs are recorded, never run."""
    from app.jobs import tasks
    calls = {"publish": [], "render": []}
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda args, delay=0: calls["publish"].append(args))
    monkeypatch.setattr(tasks.render_short, "schedule", lambda args, delay=0: calls["render"].append(args))
    return calls


# ── the Shorts factory ───────────────────────────────────────────────────────

def test_the_factory_offers_destinations_and_every_language(networks, client):
    _with_ja()
    page = client.get("/shorts").text
    for dest in ("telegram-ja", "telegram-ru", "discord-ja", "youtube", "tiktok", "instagram", "vk"):
        assert f'value="{dest}"' in page
    assert 'data-platform="vk_video"' in page and 'data-platform="ig_reels"' in page
    assert 'value="facebook-ja"' not in page  # not a network the factory posts to
    assert '"code": "ja"' in page  # a title, description and hashtags per language
    assert 'value="yt"' not in page  # no platform checkboxes any more
    # the defaults stay: YouTube, TikTok and Instagram ticked, the rest not
    tiktok = page.split('value="tiktok"', 1)[1].split(">", 1)[0]
    telegram = page.split('value="telegram-ja"', 1)[1].split(">", 1)[0]
    assert "checked" in tiktok and "checked" not in telegram


def _render(session, monkeypatch, tmp_path, brief_id):
    """Run render_short for the factory's render job of this brief, without ffmpeg."""
    from app.jobs import tasks
    from app.services import video_edit, video_editor
    job = next(j for j in session.exec(select(Job).where(Job.kind == JobKindEnum.render)).all()
               if json.loads(j.payload_json)["brief_id"] == brief_id)
    monkeypatch.setattr(video_edit, "render_file", lambda src, out, spec, info=None, **kw: open(out, "wb").write(b"v"))
    monkeypatch.setattr(video_editor, "extract_poster_frame", lambda *a, **k: None)
    return job, tasks.render_short.call_local(job.id, brief_id)


def _factory_submit(client, monkeypatch, tmp_path, **card):
    from app.services import video_edit
    from tests.test_shorts_editor import FAKE_INFO
    monkeypatch.setattr(video_edit, "probe", lambda path: dict(FAKE_INFO))
    monkeypatch.chdir(tmp_path)
    os.makedirs("media", exist_ok=True)
    (tmp_path / "media" / "short_abc_clip.mp4").write_bytes(b"src")
    meta = [{"src_rel": "short_abc_clip.mp4", "spec": {"format": "vertical"}, **card}]
    return client.post("/shorts", data={"meta": json.dumps(meta)}, follow_redirects=False)


def test_a_factory_clip_goes_to_each_destination_in_its_language(networks, client, session, monkeypatch,
                                                                 tmp_path, scheduled):
    _with_ja()
    texts = {"ru": {"title": "Ночной рейд", "desc": "Рейдеры у каравана {steam}", "tags": "#инди"},
             "en": {"title": "Night raid", "desc": "Raiders hit the caravan", "tags": "#indiegame"},
             "ja": {"title": "夜の襲撃", "desc": "キャラバンを襲う", "tags": "#インディーゲーム"}}
    r = _factory_submit(client, monkeypatch, tmp_path, texts=texts,
                        destinations=["youtube", "telegram-ja", "telegram-ru", "vk", "gone"])
    assert r.status_code == 303 and r.headers["location"].startswith("/shorts/board?skipped=gone")
    brief_id = scheduled["render"][0][1]
    brief = reload(Brief, brief_id)
    # the brief: the title and hashtags of the language written in (RU, the first), a description per language
    assert (brief.title, brief.tags_json) == ("Ночной рейд", "#инди")
    assert languages.brief_texts(brief) == {"ru": "Рейдеры у каравана {steam}", "en": "Raiders hit the caravan",
                                           "ja": "キャラバンを襲う"}
    job, result = _render(session, monkeypatch, tmp_path, brief_id)
    assert result.endswith("posts=4")
    assert json.loads(job.payload_json)["destinations"] == ["youtube", "telegram-ja", "telegram-ru", "vk"]
    posts = session.exec(select(Post).where(Post.brief_id == brief_id).order_by(Post.id)).all()
    assert [(p.platform.value, p.destination_id) for p in posts] == [
        ("yt", "youtube"), ("tg", "telegram-ja"), ("tg", "telegram-ru"), ("vk_video", "vk")]
    got = {}
    for p in posts:
        b = brief_for_publish(session, p)
        got[p.destination_id] = (b.title, post_text_lang(b, p)[0], b.tags_json)
    assert got == {
        "youtube": ("Night raid", "Raiders hit the caravan", "#indiegame"),       # YouTube is EN
        "telegram-ja": ("夜の襲撃", "キャラバンを襲う", "#インディーゲーム"),
        "telegram-ru": ("Ночной рейд", "Рейдеры у каравана", "#инди"),        # {steam}: no app id, removed
        "vk": ("Ночной рейд", "Рейдеры у каравана", "#инди"),
    }
    assert posts[2].overrides_json is None  # the brief's own language: nothing of its own
    for job_id, post_id in scheduled["publish"]:
        assert json.loads(reload(Job, job_id).payload_json)["media_rel"] == reload(Brief, brief_id).media_vertical_path
    board = client.get("/shorts/board/partial").text
    assert 'title="telegram-ja">tg JA<' in board and 'title="telegram-ru">tg RU<' in board  # two channels apart


def test_a_language_without_texts_falls_back_and_own_only_destinations_are_left_out(networks, client, session,
                                                                                     monkeypatch, tmp_path, scheduled):
    _with_ja()
    r = _factory_submit(client, monkeypatch, tmp_path,
                        texts={"ru": {"title": "Рейд", "desc": "Только по-русски", "tags": ""}, "ja": {}},
                        destinations=["telegram-ja", "discord-en"])
    board = client.get(r.headers["location"]).text
    assert "Left out of this batch" in board and "no EN text" in board
    brief_id = scheduled["render"][0][1]
    _render(session, monkeypatch, tmp_path, brief_id)
    post = session.exec(select(Post).where(Post.brief_id == brief_id)).one()
    b = brief_for_publish(session, post)
    assert post.destination_id == "telegram-ja" and (b.title, post_text_lang(b, post)) == \
        ("Рейд", ("Только по-русски", "ru"))


def test_a_destination_turned_off_before_the_render_gets_no_post(networks, client, session, monkeypatch,
                                                                  tmp_path, scheduled):
    _with_ja()
    _factory_submit(client, monkeypatch, tmp_path, texts={"en": {"title": "Clip", "desc": "d", "tags": ""}},
                    destinations=["telegram-ja", "tiktok"])
    items = [dict(d, module=g["module"]) for g in destinations.editor_context()["groups"] for d in g["destinations"]]
    for it in items:
        it["enabled"] = it["id"] != "telegram-ja"
    assert destinations.save(["ru", "en", "ja"], items)["ok"]
    brief_id = scheduled["render"][0][1]
    _render(session, monkeypatch, tmp_path, brief_id)
    assert [p.destination_id for p in session.exec(select(Post).where(Post.brief_id == brief_id))] == ["tiktok"]


def test_an_old_tab_or_a_render_queued_before_destinations_still_uses_platforms(networks, client, session,
                                                                               monkeypatch, tmp_path, scheduled):
    _with_ja()
    _factory_submit(client, monkeypatch, tmp_path, title="Old tab", text_en="Old text", tags="#a",
                    platforms=["yt", "tg"])
    brief_id = scheduled["render"][0][1]
    brief = reload(Brief, brief_id)
    assert (brief.title, brief.text_en, brief.tags_json) == ("Old tab", "Old text", "#a")
    _render(session, monkeypatch, tmp_path, brief_id)
    posts = session.exec(select(Post).where(Post.brief_id == brief_id).order_by(Post.id)).all()
    assert [(p.platform.value, p.destination_id, p.overrides_json) for p in posts] == [("yt", None, None),
                                                                                      ("tg", None, None)]


def test_translate_takes_any_configured_language(client, monkeypatch):
    from app.services import repost
    monkeypatch.setattr(repost, "translate", lambda data, target: {"title": f"{target}:{data['title']}"})
    assert client.post("/api/shorts/translate", json={"title": "Hi", "target": "日本"}).status_code == 400
    assert client.post("/api/shorts/translate", json={"title": "Hi", "target": "DE"}).json() == {"title": "de:Hi"}


# ── the TikTok pipeline ──────────────────────────────────────────────────────

def _tiktok_clip(session, tmp_path, monkeypatch, lang):
    from app.models.models import TikTokItem
    from app.services import video_edit
    media = tmp_path / "media"
    media.mkdir(exist_ok=True)
    (media / "tt_clip_9x16.mp4").write_bytes(b"v")
    monkeypatch.setattr(video_edit, "MEDIA_DIR", str(media))
    brief = Brief(user_id=1, type="video", title="Convoy", text_en="Escorting the convoy", vertical_916=True,
                  media_path="tt_clip.mp4", media_vertical_path="tt_clip_9x16.mp4")
    session.add(brief)
    session.commit()
    item = TikTokItem(brief_id=brief.id, lang=lang, caption="護衛ミッション" if lang == "ja" else "Escort mission",
                      tags="#インディーゲーム", video_rel="tt_clip_9x16.mp4")
    session.add(item)
    session.commit()
    session.refresh(item)
    return item


def _tiktok_lang(lang):
    destinations.migrate_settings()
    items = [dict(d, module=g["module"]) for g in destinations.editor_context()["groups"] for d in g["destinations"]]
    for it in items:
        if it["module"] == "tiktok":
            it["lang"] = lang
    assert destinations.save(["ru", "en", "ja"], items)["ok"]


def test_a_tiktok_clip_goes_to_the_tiktok_destination_of_its_language(session, tmp_path, monkeypatch, set_settings,
                                                                      scheduled):
    from app.services import tiktok_pipeline as tp
    from tests.test_tiktok_pipeline import CONNECTED
    set_settings(**CONNECTED)
    _tiktok_lang("ja")
    item = _tiktok_clip(session, tmp_path, monkeypatch, "ja")
    tp.send(session, item, load_settings())
    post = session.get(Post, item.post_id)
    assert post.destination_id == "tiktok"
    assert post_text_lang(brief_for_publish(session, post), post) == ("護衛ミッション\n\n#インディーゲーム", "ja")
    # a clip in another language than the TikTok destination's: no destination, exactly as before
    other = _tiktok_clip(session, tmp_path, monkeypatch, "en")
    tp.link(session, other, str((1_760_000_000 << 32) + 777))
    post = session.get(Post, other.post_id)
    assert post.destination_id is None and json.loads(post.overrides_json)["text_en"] == "Escort mission\n\n#インディーゲーム"


def test_a_factory_post_to_a_tiktok_destination_is_adopted_with_its_language(session, tmp_path, monkeypatch,
                                                                            set_settings):
    from datetime import datetime, timedelta
    from app.models.models import PostStatusEnum, TikTokItem
    from app.services import tiktok_pipeline as tp
    from tests.test_tiktok_pipeline import CONNECTED
    set_settings(**CONNECTED, languages=["ru", "en", "ja"])
    _tiktok_lang("ja")
    cfg = dict(load_settings(), tiktok_connected_at=int((datetime.utcnow() - timedelta(hours=1)).timestamp()))
    tp.adopt_factory_posts(session, cfg)  # TikTok posts other tests left in the shared DB
    brief = Brief(user_id=1, type="video", title="夜の襲撃", vertical_916=True, media_path="f.mp4",
                  media_vertical_path="f_9x16.mp4")
    languages.set_brief_texts(brief, {"ru": "Ночной рейд", "ja": "キャラバンを襲う"})
    session.add(brief)
    session.commit()
    post = Post(user_id=1, brief_id=brief.id, platform="tiktok", destination_id="tiktok",
                status=PostStatusEnum.awaiting_finish, created_at=datetime.utcnow())
    session.add(post)
    session.commit()
    assert tp.adopt_factory_posts(session, cfg) == 1
    item = session.exec(select(TikTokItem).where(TikTokItem.post_id == post.id)).one()
    assert (item.caption, item.lang) == ("キャラバンを襲う", "ja")


# ── the post-edit API ────────────────────────────────────────────────────────

def test_the_post_editor_edits_every_language(networks, client, session):
    from tests.test_destinations import _post
    _with_ja()
    pid, _ = _post(session, "tg", "telegram-ja", ru="Привет", en="Hello", ja="こんにちは", de="Hallo")
    data = client.get(f"/api/posts/{pid}/edit_data").json()
    assert [lang["code"] for lang in data["languages"]] == ["ru", "en", "ja", "de"]  # Settings' order, then the rest
    assert data["brief"]["texts"] == {"ru": "Привет", "en": "Hello", "ja": "こんにちは", "de": "Hallo"}
    assert data["post"]["lang"] == "ja" and data["brief"]["text_ru"] == "Привет"  # the old keys stay
    r = client.post(f"/api/posts/{pid}/update", data={"text_ja": "やあ", "text_en": "Hi", "tags": "#x"})
    assert r.json()["ok"]
    brief = reload(Brief, reload(Post, pid).brief_id)
    assert languages.brief_texts(brief) == {"ru": "Привет", "en": "Hi", "ja": "やあ", "de": "Hallo"}
    assert (brief.text_en, brief.tags_json) == ("Hi", "#x")
    # an older page sending only RU / EN
    client.post(f"/api/posts/{pid}/update", data={"text_ru": "Здравствуй"})
    assert languages.brief_texts(reload(Brief, brief.id))["ja"] == "やあ"
    assert reload(Brief, brief.id).text_ru == "Здравствуй"


def test_the_posts_page_builds_the_language_fields_and_opens_the_calendars_edit(client):
    assert "edit=" in client.get("/posts").text and "openEditModal(editId)" in client.get("/posts").text
    table = client.get("/posts/partial").text
    assert 'id="edit_texts"' in table and 'id="edit_text_ru"' not in table


# ── 🩺 health check ──────────────────────────────────────────────────────────

def _mock_http(monkeypatch, handler):
    """Every httpx.Client answers from `handler` (a MockTransport): nothing leaves the machine."""
    import httpx
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda *a, **kw: real(*a, transport=httpx.MockTransport(handler), **kw))


def _checked(fn_name, cfg=None):
    from app.services import health_check
    r = health_check._Result(fn_name, fn_name)
    getattr(health_check, f"check_{fn_name}")(cfg or load_settings(), r)
    return r


def test_the_health_check_checks_every_telegram_channel_and_discord_webhook(networks, monkeypatch):
    import httpx
    _with_ja(**{"discord-ja": {"enabled": False}})
    seen = []

    def api(req):
        seen.append(str(req.url))
        if "/getMe" in req.url.path:
            return httpx.Response(200, json={"ok": True, "result": {"id": 7, "username": "ipbot"}})
        if "/getChatMember" in req.url.path:
            return httpx.Response(200, json={"ok": True, "result": {"status": "administrator"}})
        if "/getChat" in req.url.path:
            names = {"-100111": "Канал", "-100222": "チャンネル"}
            chat = req.url.params["chat_id"]
            return httpx.Response(200, json={"ok": True, "result": {"title": names[chat], "type": "channel"}})
        return httpx.Response(200, json={"name": req.url.path.rsplit("/", 1)[-1], "channel_id": "1"})  # a webhook

    _mock_http(monkeypatch, api)
    r = _checked("telegram")
    assert r.status == "ok" and r.summary == "“Канал”, “チャンネル”"
    assert any("Telegram · JA: chat “チャンネル”" in d for d in r.details)
    assert any(d.startswith("✓ Telegram · RU: chat") for d in r.details)  # named once there are several
    r = _checked("discord")
    hooks = [u for u in seen if "hook/" in u]
    # the three old keys as before; the JA webhook is turned off: noted, not called
    assert sorted(hooks) == ["https://hook/default", "https://hook/en", "https://hook/ru"]
    assert any("Discord · JA: turned off" in d for d in r.details)


def test_the_health_check_checks_every_facebook_page_and_subreddit(networks, monkeypatch):
    import httpx
    from app.services import reddit_client
    _with_ja()
    pages = []

    def graph(req):
        pid = req.url.path.split("/")[2]
        pages.append((pid, req.url.params["access_token"]))
        return httpx.Response(200, json={"id": pid, "name": f"Page {pid}", "data": []})

    _mock_http(monkeypatch, graph)
    r = _checked("facebook")
    assert {p for p, _ in pages} == {"11", "22", "33"} and ("33", "tok-ja") in pages
    assert "Page 33" in r.summary and any("Facebook · JA: page “Page 33”" in d for d in r.details)

    class FakeReddit:
        def __init__(self, *a):
            self.reddit = self
            self.user = self

        def me(self):
            return type("Me", (), {"name": "dev", "link_karma": 1, "comment_karma": 1})()

        def subreddit(self, name):
            subs.append(name)
            return type("Sub", (), {"user_is_banned": False, "subscribers": 5})()

    subs = []
    monkeypatch.setattr(reddit_client, "RedditClient", FakeReddit)
    _checked("reddit")
    assert subs == ["gamedev", "igrodelo", "IndieGamesJP"]


def test_the_health_check_of_a_setup_without_destinations_is_unchanged(set_settings, monkeypatch):
    """Only the old keys (a fresh install): the same calls and lines as before."""
    import httpx
    set_settings(tg_bot_token="1:a", tg_chat_id_ru="-100111")

    def api(req):
        if "/getMe" in req.url.path:
            return httpx.Response(200, json={"ok": True, "result": {"id": 7, "username": "ipbot"}})
        if "/getChatMember" in req.url.path:
            return httpx.Response(200, json={"ok": True, "result": {"status": "administrator"}})
        return httpx.Response(200, json={"ok": True, "result": {"title": "Канал", "type": "channel"}})

    _mock_http(monkeypatch, api)
    r = _checked("telegram")
    assert (r.status, r.summary) == ("ok", "“Канал”")
    assert r.details == ["✓ bot @ipbot", "✓ chat “Канал” (channel)", "✓ bot is an admin with posting rights"]


# ── follower counts ──────────────────────────────────────────────────────────

def test_a_destination_with_its_own_channel_is_counted_on_its_own(set_settings):
    from app.services import audience
    set_settings(**OWNER, reddit_client_id="id", reddit_client_secret="s", reddit_username="u", reddit_password="p")
    _with_ja()
    own = {ch.key: ch for ch in audience.destination_channels(load_settings())}
    # the destinations migrated from the old keys keep the networks' channels (and their history)
    assert sorted(own) == ["facebook:facebook-ja", "reddit:reddit-ja", "telegram:telegram-ja"]
    assert own["telegram:telegram-ja"].metric == "tg_subscribers:telegram-ja"
    assert own["facebook:facebook-ja"].metric == "fb_followers:facebook-ja"
    assert own["facebook:facebook-ja"].label == "Facebook · JA"
    assert [ch.metric for ch in audience.channels(load_settings())][:len(audience.CHANNELS)] == audience.FOLLOWER_METRICS
    assert audience.is_follower_metric("tg_subscribers:telegram-ja") and not audience.is_follower_metric("x:y")
    from app.services import marketing as mk
    assert mk.metric_kind("fb_followers:facebook-ja") == "level" and mk.metric_kind("fb_ru_followers") == "level"


def test_followers_of_every_channel_are_collected_and_summed(set_settings, session, monkeypatch, client):
    from sqlalchemy import delete
    from app.models.models import AudienceSample, MetricPoint
    from app.services import marketing as mk
    from app.services import marketing_collect as mc
    from tests.test_audience import FakeClient, Resp
    session.execute(delete(AudienceSample))
    session.execute(delete(MetricPoint).where(mc._follower_metrics()))
    session.execute(delete(MetricPoint).where(MetricPoint.metric == "followers_total"))
    session.commit()
    set_settings(**OWNER)
    _with_ja()

    def tg(url, kw):
        return Resp({"ok": True, "result": {"-100111": 120, "-100222": 30}[kw["params"]["chat_id"]]})

    def fb(url, kw):
        page = url.rsplit("/", 1)[-1]
        return Resp({"name": f"Page {page}", "followers_count": {"11": 1000, "22": 200, "33": 7}[page]})

    fake = FakeClient({"getChatMemberCount": tg, "graph.facebook.com": fb})
    monkeypatch.setattr(mc, "_with_client", lambda fn: fn(fake))
    monkeypatch.setattr(mc, "steam_projects", lambda s: [])
    summary = mc.collect_audience(session, load_settings(), mc.Budget(5), force=True)
    assert set(summary) == {"tg", "fb_ru", "fb_en", "telegram:telegram-ja", "facebook:facebook-ja"}
    today = mk.today_str()
    level = {m: mk.level_on_or_before(session, 0, m, today).value for m in (
        "tg_subscribers", "tg_subscribers:telegram-ja", "fb_ru_followers", "fb_en_followers", "fb_followers:facebook-ja")}
    assert level == {"tg_subscribers": 120, "tg_subscribers:telegram-ja": 30, "fb_ru_followers": 1000,
                     "fb_en_followers": 200, "fb_followers:facebook-ja": 7}
    assert mk.level_on_or_before(session, 0, "followers_total", today).value == 1357
    statuses = {s["source"]: s for s in mc.source_statuses(session)}
    assert statuses["telegram:telegram-ja"]["label"] == "Telegram · JA" and statuses["telegram:telegram-ja"]["audience"]
    home = client.get("/")
    assert home.status_code == 200 and "Telegram · JA" in home.text  # its own row among the followers


def test_a_publication_counts_the_channels_it_went_to(set_settings):
    from app.services import audience
    set_settings(**OWNER)
    _with_ja()
    chans = audience.channels(load_settings())

    def went(*posts):
        return audience.reached(chans, [Post(user_id=1, brief_id=1, platform=p, destination_id=d) for p, d in posts])

    assert went(("tg", "telegram-ja")) == {"telegram:telegram-ja"}
    assert went(("tg", "telegram-ru")) == {"tg"} == went(("tg", None))  # the RU channel, as before
    assert went(("fb_en", "facebook-ja")) == {"facebook:facebook-ja"}  # not the EN Page
    assert went(("fb_en", "facebook-en"), ("vk", None)) == {"fb_en", "vk"}


# ── the old /news page ───────────────────────────────────────────────────────

def test_the_old_news_page_redirects_and_its_form_still_posts(client, session, set_settings, scheduled):
    r = client.get("/news", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/create/post"
    set_settings(tg_bot_token="1:a", tg_chat_id_ru="-100111")
    r = client.post("/news", data={"text_ru": "Старая вкладка", "text_en": "Old tab", "platforms": ["tg"]},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/published/")
    brief_id = int(r.headers["location"].split("/published/")[1].split("?")[0])
    post = session.exec(select(Post).where(Post.brief_id == brief_id)).one()
    assert (post.platform.value, post.destination_id) == ("tg", None) and len(scheduled["publish"]) == 1


# ── a fresh install: networks set up later, through their old Settings sections ──

def _sent_to_telegram(monkeypatch):
    from app.services import telegram_client
    from tests.test_destinations import SENT, _Rec
    monkeypatch.setattr(telegram_client, "TelegramClient", _Rec)
    SENT.clear()
    return SENT


def test_a_fresh_install_publishes_to_a_telegram_channel_set_up_later(client, session, monkeypatch, scheduled,
                                                                     settings_file):
    """Empty settings: the start migrates them (no chat id yet); the old Telegram fields then fill the linked
    destination and a post goes there."""
    assert json.loads(settings_file.read_text(encoding="utf-8"))["destinations"]  # the app's start migrated
    assert not destinations.ready(destinations.get("telegram-ru"))
    client.post("/api/settings/save", data={"tg_bot_token": "1:abc", "tg_chat_id_ru": "-100500"})
    dest = destinations.get("telegram-ru")
    assert dest["target"] == {"chat_id": "-100500"} and destinations.ready(dest)
    page = client.get("/create/post").text
    assert 'value="telegram-ru"' in page
    r = client.post("/create/post", data={"text_ru": "Первый пост", "destinations": ["telegram-ru"]},
                    follow_redirects=False)
    brief_id = int(r.headers["location"].split("/published/")[1].split("?")[0])
    post = session.exec(select(Post).where(Post.brief_id == brief_id)).one()
    sent = _sent_to_telegram(monkeypatch)
    job = next(j for j in session.exec(select(Job)).all() if json.loads(j.payload_json).get("post_id") == post.id)
    assert _publish((post.id, job.id)) == "ok"
    assert sent == [("tg", "-100500", "Первый пост")]


def test_a_discord_webhook_typed_into_the_single_field_reaches_a_destination(client, set_settings):
    # a fresh install: the single webhook's destination is there, linked
    client.post("/api/discord/webhook", data={"url": "https://hook/one"})
    assert destinations.get("discord")["target"] == {"webhook": "https://hook/one"}
    # an install migrated with RU/EN webhooks has no destination for the single field: typing one adds it
    set_settings(destinations=None, discord_webhook_url="", discord_webhook_ru="https://hook/ru",
                 discord_webhook_en="https://hook/en")
    destinations.migrate_settings()
    assert destinations.get("discord") is None
    client.post("/api/discord/webhook", data={"url": "https://hook/single"})
    dest = destinations.get("discord")
    assert dest["target"] == {"webhook": "https://hook/single"} and dest["linked"] == {"webhook": "discord_webhook_url"}
    assert destinations.ready(dest) and "discord" in [d["id"] for g in destinations.for_form() for d in g["destinations"]]
    # saved again as it was: nothing new
    ids = [d["id"] for d in destinations.stored()]
    client.post("/api/discord/webhook", data={"url": "https://hook/single"})
    client.post("/api/discord/webhooks/save", data={"url_ru": "https://hook/ru"})
    assert [d["id"] for d in destinations.stored()] == ids


def test_an_old_field_never_doubles_a_destination_and_follows_the_languages(client, set_settings):
    # the owner's setup: saving the old single-webhook field with the value it had adds nothing
    set_settings(**OWNER)
    destinations.migrate_settings()
    before = [d["id"] for d in destinations.stored()]
    client.post("/api/discord/webhook", data={"url": "https://hook/default"})
    assert [d["id"] for d in destinations.stored()] == before
    # a destination removed in the editor comes back when its old field is set up again, in a language of Settings
    items = [dict(d, module=g["module"]) for g in destinations.editor_context()["groups"] for d in g["destinations"]
             if d["id"] != "telegram-ru"]
    assert destinations.save(["en", "ja"], [dict(i, lang="en") for i in items])["ok"]
    client.post("/api/settings/save", data={"tg_chat_id_ru": "-100777"})
    back = destinations.get("telegram-ru")
    assert back["target"] == {"chat_id": "-100777"} and back["lang"] == "en"
    # ...and the editor still saves (its language is one of the list)
    items = [dict(d, module=g["module"]) for g in destinations.editor_context()["groups"] for d in g["destinations"]]
    assert destinations.save(["en", "ja"], items)["ok"]
    # a target that already has a destination of its own isn't added twice
    items.append({"module": "reddit", "lang": "ja", "target": {"subreddit": "IndieGamesJP"}})
    assert destinations.save(["en", "ja"], items)["ok"]
    before = [d["id"] for d in destinations.stored()]
    client.post("/api/settings/save", data={"reddit_subreddit_en": "IndieGamesJP"})
    assert [d["id"] for d in destinations.stored()] == before


def test_use_as_ru_brings_back_the_facebook_page(client, set_settings):
    destinations.migrate_settings()
    items = [dict(d, module=g["module"]) for g in destinations.editor_context()["groups"] for d in g["destinations"]
             if not d["id"].startswith("facebook")]
    assert destinations.save(["ru", "en"], items)["ok"]
    client.post("/api/facebook/assign_page", data={"lang": "ru", "page_id": "11", "page_token": "tok", "page_name": "P"})
    dest = destinations.get("facebook-ru")
    assert dest["target"] == {"page_id": "11", "page_token": "tok"} and dest["platform"] == "fb_ru"
    assert [d["id"] for d in destinations.stored()].count("facebook-ru") == 1
