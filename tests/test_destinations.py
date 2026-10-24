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
