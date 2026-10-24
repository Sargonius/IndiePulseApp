"""Languages & destinations: N languages per publication and per-target languages, migrated from the RU/EN keys."""
import json

import pytest
from sqlmodel import Session

from app.db.session import engine, migrate_db
from app.models.models import Brief
from app.services import destinations, languages
from app.services.settings import load_settings
from tests.conftest import reload


def _ids(cfg=None):
    return [d["id"] for d in destinations.stored(cfg or load_settings())]


# ── the owner's setup today, as the migration sees it ───────────────────────

OWNER = dict(
    tg_bot_token="123:abc", tg_chat_id_ru="-100111",
    discord_webhook_url="https://hook/default", discord_webhook_ru="https://hook/ru", discord_webhook_en="https://hook/en",
    fb_ru_page_id="11", fb_ru_page_access_token="tok-ru", fb_en_page_id="22", fb_en_page_access_token="tok-en",
    reddit_subreddit_en="gamedev", reddit_subreddit_ru="igrodelo",
)


def test_migration_builds_destinations_from_the_per_language_keys(set_settings):
    set_settings(**OWNER)
    assert destinations.migrate_settings() is True
    cfg = load_settings()
    assert cfg["languages"] == ["ru", "en"]
    by_id = {d["id"]: d for d in destinations.all_destinations(cfg)}
    assert list(by_id) == ["vk", "x", "discord-ru", "discord-en", "facebook-ru", "facebook-en", "bluesky",
                           "reddit-ru", "reddit-en", "telegram-ru", "youtube", "tiktok", "instagram"]
    langs = {i: d["lang"] for i, d in by_id.items()}
    assert langs == {"vk": "ru", "x": "en", "discord-ru": "ru", "discord-en": "en", "facebook-ru": "ru",
                     "facebook-en": "en", "bluesky": "en", "reddit-ru": "ru", "reddit-en": "en",
                     "telegram-ru": "ru", "youtube": "en", "tiktok": "en", "instagram": "en"}
    # the targets are the old keys' values (linked, not copied)
    assert by_id["telegram-ru"]["target"] == {"chat_id": "-100111"}
    assert by_id["discord-en"]["target"] == {"webhook": "https://hook/en"}
    assert by_id["facebook-ru"]["target"] == {"page_id": "11", "page_token": "tok-ru"}
    assert by_id["facebook-ru"]["platform"] == "fb_ru" and by_id["facebook-en"]["platform"] == "fb_en"
    # the default webhook got nothing while the RU/EN ones were set; it stays out
    assert "discord" not in by_id
    # Discord RU/EN only ever got their own language's text
    assert by_id["discord-ru"]["skip_without_text"] and not by_id["telegram-ru"]["skip_without_text"]
    # the RU subreddit was never posted to: it comes in, but turned off
    assert by_id["reddit-ru"]["enabled"] is False and by_id["reddit-en"]["enabled"] is True
    # the old keys are all still there (rollback)
    for key, value in OWNER.items():
        assert cfg[key] == value


def test_migration_is_idempotent_and_never_rebuilds_the_users_list(set_settings):
    set_settings(**OWNER)
    assert destinations.migrate_settings() is True
    first = load_settings()["destinations"]
    assert destinations.migrate_settings() is False
    migrate_db()  # what both the server and the worker run at start
    assert load_settings()["destinations"] == first
    # the user's own list (even an empty one) is never rebuilt
    destinations.save(["ru", "en"], [])
    migrate_db()
    assert load_settings()["destinations"] == []


def test_migration_of_a_fresh_install_offers_the_settings_slots(settings_file):
    assert load_settings()["destinations"] is None  # not migrated: computed on the fly
    assert "telegram-ru" in _ids()
    destinations.migrate_settings()
    stored = json.loads(settings_file.read_text(encoding="utf-8"))
    ids = [d["id"] for d in stored["destinations"]]
    # nothing set: the single Discord webhook, both Facebook slots and the EN subreddit of the Settings page
    assert "discord" in ids and "facebook-ru" in ids and "facebook-en" in ids
    assert "reddit-en" in ids and "reddit-ru" not in ids
    assert stored["languages"] == ["ru", "en"]


def test_a_linked_destination_follows_its_old_key(set_settings):
    set_settings(**OWNER)
    destinations.migrate_settings()
    # the old Settings field / the Facebook "fetch pages" flow write the old key...
    set_settings(tg_chat_id_ru="-100999", fb_en_page_access_token="tok-en-2")
    assert destinations.get("telegram-ru")["target"]["chat_id"] == "-100999"
    assert destinations.get("facebook-en")["target"]["page_token"] == "tok-en-2"
    # ...and the editor writes it back there
    editor = destinations.editor_context()
    items = [dict(d, module=g["module"]) for g in editor["groups"] for d in g["destinations"]]
    for it in items:
        if it["id"] == "telegram-ru":
            it["target"] = {"chat_id": "-100777"}
    assert destinations.save(["ru", "en"], items)["ok"]
    cfg = load_settings()
    assert cfg["tg_chat_id_ru"] == "-100777"
    # a secret left empty in the editor kept its value
    assert cfg["fb_en_page_access_token"] == "tok-en-2"
    assert destinations.get("facebook-en", cfg)["target"]["page_token"] == "tok-en-2"


def test_the_editor_adds_a_language_and_a_destination(set_settings):
    set_settings(**OWNER)
    destinations.migrate_settings()
    items = [{"id": d["id"], "module": d["module"], "lang": d["lang"], "enabled": d["enabled"],
              "skip_without_text": d["skip_without_text"], "target": {}}
             for d in destinations.stored(load_settings())]
    items.append({"module": "telegram", "lang": "ja", "label": "JP channel", "target": {"chat_id": "-100222"}})
    bad = destinations.save(["ru", "en"], items)
    assert not bad["ok"] and "isn't in the languages list" in bad["errors"][0]
    res = destinations.save(["ru", "en", "JA"], items)
    assert res["ok"], res
    cfg = load_settings()
    assert cfg["languages"] == ["ru", "en", "ja"]
    ja = destinations.get("telegram-ja", cfg)
    assert ja["target"] == {"chat_id": "-100222"} and ja["label"] == "JP channel" and ja["linked"] == {}
    assert cfg["tg_chat_id_ru"] == "-100111"  # a field not sent keeps its value


def test_one_account_networks_get_one_destination():
    res = destinations.save(["ru", "en"], [{"module": "x", "lang": "en"}, {"module": "x", "lang": "ru"}])
    assert not res["ok"] and "one account" in res["errors"][0]


# ── brief texts per language ────────────────────────────────────────────────

def test_brief_texts_mirror_text_ru_and_text_en_both_ways(session):
    b = Brief(user_id=1, type="post", text_ru="Привет", text_en="Hello")
    languages.set_brief_texts(b, {"ja": "こんにちは"})
    session.add(b)
    session.commit()
    stored = reload(Brief, b.id)
    assert json.loads(stored.texts_json) == {"ru": "Привет", "en": "Hello", "ja": "こんにちは"}
    # untouched code writing the column directly: texts_json follows on save
    with Session(engine) as s:
        row = s.get(Brief, b.id)
        row.text_en = "Hi"
        s.add(row)
        s.commit()
    stored = reload(Brief, b.id)
    assert json.loads(stored.texts_json)["en"] == "Hi"
    # and the accessor writes the columns
    languages.set_brief_texts(stored, {"ru": "Здравствуй"})
    assert stored.text_ru == "Здравствуй" and languages.brief_texts(stored)["ja"] == "こんにちは"


def test_an_old_brief_without_texts_json_reads_from_its_columns(session):
    b = Brief(user_id=1, type="post", text_ru="Старый", text_en="Old")
    b.texts_json = None
    assert languages.brief_texts(b) == {"ru": "Старый", "en": "Old"}
    assert languages.brief_text(b, "ja", {"languages": ["ru", "en"]}) == "Старый"  # → the first language
    assert languages.brief_text(b, "en", {"languages": ["ru", "en"]}) == "Old"
    b2 = Brief(user_id=1, type="post", text_ru="", text_en="Only EN")
    assert languages.text_with_lang(b2, "ja", {"languages": ["ru", "en"]}) == ("Only EN", "en")  # → any text


@pytest.mark.parametrize("code,ok", [("ja", "ja"), ("JA", "ja"), ("pt_BR", "pt-br"), ("j", None), ("日本", None)])
def test_language_codes(code, ok):
    assert languages.normalize_code(code) == ok


# ── publishing by destination ───────────────────────────────────────────────

SENT: list = []


class _Rec:
    """One fake for every network client: records (network, target, text)."""

    def __init__(self, *args, **kw):
        self.args = args
        self.last_channel_id = "c"
        self.last_message_id = None
        self.reddit = self

    # Telegram: send_message(chat_id, text); Discord: send_message(content, file_paths=...)
    def send_message(self, *args, **kw):
        if len(args) == 2:
            SENT.append(("tg", args[0], args[1]))
            return {"result": {"message_id": len(SENT)}}
        SENT.append(("discord", self.args[0], args[0]))
        self.last_message_id = str(len(SENT))
        return self.last_message_id, None

    # Facebook
    def create_post(self, page_id, message):
        SENT.append(("fb", (page_id, self.args[0]), message))
        return {"id": f"{page_id}_1"}

    def get_permalink(self, post_id):
        return f"https://facebook.com/{post_id}"

    # Reddit
    def submit_text(self, sub, title, text):
        SENT.append(("reddit", sub, title))
        return "abc", "https://reddit.com/abc"

    def submission(self, id):
        return type("S", (), {"permalink": f"/r/x/comments/{id}"})()

    # VK
    def wall_post(self, owner_id, message):
        SENT.append(("vk", owner_id, message))
        return {"post_id": 1}

    # Bluesky
    def post_thread(self, text, hashtags="", image_paths=None, langs=None, video_path=None):
        SENT.append(("bluesky", tuple(langs or ()), text))
        return "at://x/1", "https://bsky.app/x/1"


@pytest.fixture
def networks(monkeypatch, set_settings):
    from app.services import (bluesky_client, discord_client, facebook_client, reddit_client, telegram_client,
                              vk_client)
    for mod, name in ((telegram_client, "TelegramClient"), (discord_client, "DiscordClient"),
                      (facebook_client, "FacebookClient"), (reddit_client, "RedditClient"),
                      (vk_client, "VKClient"), (bluesky_client, "BlueskyClient")):
        monkeypatch.setattr(mod, name, _Rec)
    set_settings(**OWNER, vk_access_token="vk", vk_group_id="club42", bluesky_handle="me.bsky.social",
                 bluesky_app_password="pw", reddit_client_id="id", reddit_client_secret="s",
                 reddit_username="u", reddit_password="p")
    SENT.clear()
    return SENT


def _post(session, platform, destination_id=None, **texts):
    """A queued post of a new brief with these texts ({lang: text}) and its publish job → (post id, job id).
    The tests reuse texts in one DB: the duplicate guard is let through except where it is the point."""
    brief = Brief(user_id=1, type="post")
    languages.set_brief_texts(brief, texts)
    session.add(brief)
    session.commit()
    return _post_of(session, brief.id, platform, destination_id, allow_duplicate=True)


def _post_of(session, brief_id, platform, destination_id=None, allow_duplicate=False):
    from app.models.models import Job, JobKindEnum, Post
    from app.services import duplicates
    post = Post(user_id=1, brief_id=brief_id, platform=platform, destination_id=destination_id)
    session.add(post)
    session.commit()
    payload = {"post_id": post.id, **({duplicates.ALLOW: True} if allow_duplicate else {})}
    job = Job(kind=JobKindEnum.publish, payload_json=json.dumps(payload))
    session.add(job)
    session.commit()
    return post.id, job.id


def _publish(ids):
    from app.jobs import tasks
    return tasks._publish_news_post_impl(ids[1], ids[0])


def _with_ja(**changes):
    """The owner's destinations plus Japanese: a JA Telegram channel, Discord webhook, Facebook page, subreddit.
    `changes`: {destination id: {field: value}} applied on top."""
    destinations.migrate_settings()
    items = [{"id": d["id"], "module": d["module"], "lang": d["lang"], "enabled": d["enabled"],
              "skip_without_text": d["skip_without_text"]} for d in destinations.stored(load_settings())]
    items += [
        {"module": "telegram", "lang": "ja", "target": {"chat_id": "-100222"}},
        {"module": "discord", "lang": "ja", "target": {"webhook": "https://hook/ja"}},
        {"module": "facebook", "lang": "ja", "target": {"page_id": "33", "page_token": "tok-ja"}},
        {"module": "reddit", "lang": "ja", "target": {"subreddit": "IndieGamesJP"}},
    ]
    for it in items:
        it.update(changes.get(it.get("id") or f"{it['module']}-{it['lang']}", {}))
    res = destinations.save(["ru", "en", "ja"], items)
    assert res["ok"], res


TEXTS = dict(ru="Привет", en="Hello", ja="こんにちは")


@pytest.mark.parametrize("platform, dest, expected", [
    ("tg", "telegram-ja", ("tg", "-100222", "こんにちは")),
    ("tg", "telegram-ru", ("tg", "-100111", "Привет")),
    ("discord", "discord-ja", ("discord", "https://hook/ja", "こんにちは")),
    ("discord", "discord-en", ("discord", "https://hook/en", "Hello")),
    ("fb_en", "facebook-ja", ("fb", ("33", "tok-ja"), "こんにちは")),
    ("fb_ru", "facebook-ru", ("fb", ("11", "tok-ru"), "Привет")),
    ("reddit", "reddit-ja", ("reddit", "IndieGamesJP", "こんにちは")),
])
def test_a_post_to_a_destination_goes_there_in_its_language(session, networks, platform, dest, expected):
    _with_ja()
    assert _publish(_post(session, platform, dest, **TEXTS)) == "ok"
    assert networks == [expected]


def test_single_account_networks_take_their_destinations_language(session, networks):
    from app.models.models import Post
    from app.platforms import x as xp
    _with_ja(vk={"lang": "ja"}, bluesky={"lang": "ja"})
    assert _publish(_post(session, "vk", "vk", **TEXTS)) == "ok"
    assert _publish(_post(session, "bluesky", "bluesky", **TEXTS)) == "ok"
    assert networks == [("vk", "-42", "こんにちは"), ("bluesky", ("ja",), "こんにちは")]
    post = Post(user_id=1, brief_id=1, platform="x", destination_id="x")
    b = Brief(user_id=1, type="post")
    languages.set_brief_texts(b, TEXTS)
    assert xp.compose(b, post)[0] == "Hello"
    _with_ja(x={"lang": "ja"})
    assert xp.compose(b, post)[0] == "こんにちは"
    assert xp.compose(b)[0] == "Hello"  # no post, so no destination: EN first, as before


def test_fallback_its_language_then_the_first_then_any(session, networks):
    _with_ja()
    # no JA text: the first language's (RU) goes to the JA channel
    assert _publish(_post(session, "tg", "telegram-ja", ru="Привет", en="Hello")) == "ok"
    # no JA, no RU: any text there is
    assert _publish(_post(session, "tg", "telegram-ja", en="Hello only")) == "ok"
    assert [s[2] for s in networks] == ["Привет", "Hello only"]
    assert _publish(_post(session, "bluesky", "bluesky", ru="Только русский")) == "ok"
    assert networks[-1] == ("bluesky", ("ru",), "Только русский")  # tagged with the language it really is


def test_a_destination_for_its_own_language_only_gets_nothing_without_it(session, networks):
    from app.models.models import Post, PostStatusEnum
    _with_ja()
    ids = _post(session, "discord", "discord-en", ru="Только RU")
    assert _publish(ids) == "no_text"
    post = reload(Post, ids[0])
    assert post.status == PostStatusEnum.canceled and "no_text_in_language" in post.error_text
    assert networks == []


def test_per_post_overrides_work_per_language(session, networks):
    from app.models.models import Post
    _with_ja()
    pid, jid = _post(session, "tg", "telegram-ja", **TEXTS)
    with Session(engine) as s:
        p = s.get(Post, pid)
        p.overrides_json = json.dumps({"text_ja": "上書き"}, ensure_ascii=False)
        s.add(p)
        s.commit()
    assert _publish((pid, jid)) == "ok"
    assert networks == [("tg", "-100222", "上書き")]


def test_posts_without_a_destination_publish_exactly_as_before(session, networks):
    """Queued before destinations (or by a path that names none): the old keys, the old text order —
    even after the destinations were changed."""
    _with_ja(**{"telegram-ru": {"lang": "ja"}, "x": {"lang": "ja"}})
    assert _publish(_post(session, "tg", None, **TEXTS)) == "ok"            # RU text → tg_chat_id_ru
    assert _publish(_post(session, "discord", None, **TEXTS)) == "ok"       # one post → the RU and EN webhooks
    assert _publish(_post(session, "fb_en", None, **TEXTS)) == "ok"         # the EN page, EN text
    assert _publish(_post(session, "reddit", None, **TEXTS)) == "ok"        # the EN subreddit, EN text
    assert _publish(_post(session, "vk", None, en="Hello")) == "ok"         # RU first, else EN
    assert networks == [
        ("tg", "-100111", "Привет"),
        ("discord", "https://hook/ru", "Привет"), ("discord", "https://hook/en", "Hello"),
        ("fb", ("22", "tok-en"), "Hello"),
        ("reddit", "gamedev", "Hello"),
        ("vk", "-42", "Hello"),
    ]


def test_a_removed_or_turned_off_destination_is_never_replaced_by_another(session, networks):
    from app.models.models import Post, PostStatusEnum
    _with_ja(**{"telegram-ja": {"enabled": False}})
    ids = _post(session, "tg", "telegram-ja", **TEXTS)
    assert _publish(ids) == "destination_off"
    assert "turned off" in reload(Post, ids[0]).error_text
    ids = _post(session, "tg", "telegram-gone", **TEXTS)
    assert _publish(ids) == "destination_off"
    post = reload(Post, ids[0])
    assert post.status == PostStatusEnum.error and "removed" in post.error_text
    assert networks == []


def test_destinations_of_a_module_that_is_off_are_hidden_and_skipped(session, networks, set_settings):
    _with_ja()
    set_settings(modules_disabled=["telegram"])
    modules_in_form = [g["module"] for g in destinations.for_form("post")]
    assert "telegram" not in modules_in_form and "discord" in modules_in_form
    usable, skipped = destinations.chosen(["telegram-ja", "discord-ja"])
    assert [d["id"] for d in usable] == ["discord-ja"] and skipped == ["telegram-ja"]
    assert _publish(_post(session, "tg", "telegram-ja", **TEXTS)) == "module_off"
    assert networks == []


def test_two_destinations_of_one_platform_are_not_duplicates(session, networks):
    from app.models.models import Post, PostStatusEnum
    _with_ja()
    first = _post(session, "tg", "telegram-ru", ru="Один раз", en="Once")
    assert _publish(first) == "ok"
    brief_id = reload(Post, first[0]).brief_id
    # the same publication to a second channel is not a twin of the first...
    assert _publish(_post_of(session, brief_id, "tg", "telegram-ja")) == "ok"
    # ...the same channel again is
    again = _post_of(session, brief_id, "tg", "telegram-ja")
    assert _publish(again) == "duplicate"
    assert reload(Post, again[0]).status == PostStatusEnum.canceled
    assert [s[1] for s in networks] == ["-100111", "-100222"]


# ── Settings → Languages & destinations ─────────────────────────────────────

def test_the_settings_page_edits_languages_and_destinations(client, set_settings):
    set_settings(**OWNER)
    page = client.get("/accounts").text
    assert 'id="section_languages"' in page and "window.IP_DEST" in page
    assert "tok-ru" not in page.split("window.IP_DEST", 1)[1].split("</script>", 1)[0]  # page tokens masked
    data = client.get("/api/destinations").json()
    items = [dict(d, module=g["module"]) for g in data["groups"] for d in g["destinations"]]
    items.append({"module": "discord", "lang": "ja", "target": {"webhook": "https://hook/ja"}})
    r = client.post("/api/destinations/save", json={"languages": ["ru", "en"], "destinations": items})
    assert r.status_code == 400 and "isn't in the languages list" in r.json()["errors"][0]
    r = client.post("/api/destinations/save", json={"languages": ["ru", "en", "ja"], "destinations": items})
    assert r.status_code == 200 and r.json()["ok"]
    assert "discord-ja" in r.json()["destinations"]
    cfg = load_settings()
    assert cfg["languages"] == ["ru", "en", "ja"] and cfg["fb_ru_page_access_token"] == "tok-ru"


# ── the post form: a field per language, destinations, Translate missing ────

# (`networks` before `client`: the app's start migrates the settings it finds, so the owner's keys must be there)

def _fake_ai(monkeypatch, fail=False):
    from app.services import llm, machine_translate
    calls = []

    def chat(messages, **kw):
        calls.append(messages[0]["content"])
        if fail:
            raise llm.LLMError("no AI key in Settings")
        target = messages[1]["content"].split(",", 1)[0].replace("Translate to ", "")
        return f"(the text in {target})", "openai:test"

    monkeypatch.setattr(llm, "chat", chat)
    monkeypatch.setattr(machine_translate, "translate", lambda text, target, source=None: (f"<{target}> {text}", "google"))
    return calls


def test_an_added_language_works_end_to_end(networks, client, session, monkeypatch):
    """Add JA and a JA Telegram channel + Discord webhook → the form has a JA field, Translate missing fills
    it, and publishing sends the JA text to the JA destinations."""
    from sqlmodel import select
    from app.models.models import Job, Post
    _with_ja()
    page = client.get("/create/post").text
    assert 'name="text_ja"' in page and 'name="text_ru"' in page and 'name="text_en"' in page
    assert 'value="telegram-ja"' in page and 'value="discord-ja"' in page and 'data-lang="ja"' in page
    assert 'type="checkbox" name="platforms"' not in page

    _fake_ai(monkeypatch)
    r = client.post("/api/translate/missing", data={"text_ru": "Новый билд вышел", "text_en": "", "text_ja": ""})
    res = r.json()
    assert res["source"] == "ru" and sorted(res["texts"]) == ["en", "ja"] and res["failed"] == []
    assert res["texts"] == {"en": "(the text in English)", "ja": "(the text in Japanese)"}
    assert res["providers"]["ja"] == "openai:test"

    r = client.post("/create/post", data={
        "text_ru": "Новый билд вышел", "text_en": "New build is out", "text_ja": "新しいビルド",
        "destinations": ["telegram-ja", "discord-ja", "telegram-ru", "nope"],
    }, follow_redirects=False)
    assert r.status_code == 303 and "nope" in r.headers["location"]  # skipped, and said so
    brief_id = int(r.headers["location"].split("/published/")[1].split("?")[0])
    assert languages.brief_texts(reload(Brief, brief_id))["ja"] == "新しいビルド"
    posts = session.exec(select(Post).where(Post.brief_id == brief_id).order_by(Post.id)).all()
    assert [(p.platform.value, p.destination_id) for p in posts] == [
        ("tg", "telegram-ja"), ("discord", "discord-ja"), ("tg", "telegram-ru")]
    for p in posts:
        job = next(j for j in session.exec(select(Job)).all() if json.loads(j.payload_json).get("post_id") == p.id)
        assert _publish((p.id, job.id)) == "ok"
    assert networks == [("tg", "-100222", "新しいビルド"), ("discord", "https://hook/ja", "新しいビルド"),
                        ("tg", "-100111", "Новый билд вышел")]


def test_translate_missing_falls_back_to_machine_translation(client, monkeypatch, set_settings):
    set_settings(languages=["en", "ja", "de"])
    calls = _fake_ai(monkeypatch, fail=True)
    res = client.post("/api/translate/missing", data={"text_en": "Hello", "text_ja": "既に", "text_de": ""}).json()
    assert calls and res["texts"] == {"de": "<de> Hello"} and res["providers"] == {"de": "google"}


def test_the_form_sends_only_its_own_language_destinations_text_or_skips_them(networks, client, session):
    from sqlmodel import select
    from app.models.models import Post
    _with_ja()
    r = client.post("/create/post", data={"text_ru": "Только по-русски 1", "destinations": ["discord-en", "discord-ru"]},
                    follow_redirects=False)
    assert "no+EN+text" in r.headers["location"]
    brief_id = int(r.headers["location"].split("/published/")[1].split("?")[0])
    assert [p.destination_id for p in session.exec(select(Post).where(Post.brief_id == brief_id))] == ["discord-ru"]


def test_the_form_has_no_destinations_of_a_module_that_is_off(networks, client, set_settings):
    _with_ja()
    set_settings(modules_disabled=["telegram"])
    page = client.get("/create/post").text
    assert 'value="telegram-ja"' not in page and 'value="discord-ja"' in page


def test_the_video_form_publishes_vk_as_a_video_to_its_destination(networks, client, session):
    from sqlmodel import select
    from app.models.models import Post
    _with_ja(vk={"lang": "ja"})
    page = client.get("/create/video").text
    assert 'name="text_ja"' in page and 'value="vk"' in page and 'value="telegram-ja"' not in page
    r = client.post("/create/video", data={"title": "Clip", "text_ru": "Ролик", "text_ja": "動画",
                                           "media_horizontal": "clip.mp4", "destinations": ["vk"]},
                    follow_redirects=False)
    brief_id = int(r.headers["location"].split("/published/")[1].split("?")[0])
    post = session.exec(select(Post).where(Post.brief_id == brief_id)).one()
    assert (post.platform.value, post.destination_id) == ("vk_video", "vk")
