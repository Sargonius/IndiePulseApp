"""First run: a fresh install gets the setup wizard; an install with settings never does (app/services/setup.py)."""
import json

import pytest

from app.db.session import migrate_db
from app.services import setup
from app.services.settings import DEFAULTS, load_settings
from tests.conftest import FRESH_SETTINGS

# the settings of an install that has been in use, before this version
OWNER = {"tg_bot_token": "123:abc", "tg_chat_id_ru": "-100", "music_dir": "E:\\Music",
         "yt_playlist_title": "My game", "languages": ["ru", "en"], "update_mode": "notify"}


@pytest.fixture(autouse=True)
def no_steam_env(monkeypatch):
    """INDIEPULSE_STEAM_APP_ID from this computer's environment would stand in for the built-in default."""
    from app.core import config
    monkeypatch.setattr(config, "STEAM_APP_ID", "")


def test_clubkits_steam_app_id_is_not_indiepulses(monkeypatch):
    """ClubKit puts STEAM_APP_ID in the Windows user environment for another game's Steam builds; IndiePulse read it
    and its store-news export opened that game. Only its own INDIEPULSE_STEAM_APP_ID counts now."""
    import importlib
    from app.core import config
    from app.services import steam_links
    monkeypatch.setenv("STEAM_APP_ID", "2962320")
    monkeypatch.delenv("INDIEPULSE_STEAM_APP_ID", raising=False)
    try:
        importlib.reload(config)
        assert config.STEAM_APP_ID == "" and steam_links.app_id({}) == ""
        monkeypatch.setenv("INDIEPULSE_STEAM_APP_ID", "480")
        importlib.reload(config)
        assert steam_links.app_id({}) == "480" and steam_links.app_id({"steam_app_id": "730"}) == "730"
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def stored(settings_file) -> dict:
    return json.loads(settings_file.read_text(encoding="utf-8"))


@pytest.fixture
def fresh(settings_file):
    settings_file.write_text(FRESH_SETTINGS, encoding="utf-8")
    return settings_file


@pytest.fixture
def existing(settings_file):
    settings_file.write_text(json.dumps(OWNER), encoding="utf-8")
    return settings_file


def test_new_installs_get_neutral_defaults():
    assert DEFAULTS["music_dir"] == DEFAULTS["yt_playlist_title"] == DEFAULTS["steam_app_id"] == ""
    assert DEFAULTS["default_hashtags"] == ""


def test_an_existing_install_is_set_up_and_keeps_its_values(existing):
    assert setup.on_start() == "existing"
    cfg = stored(existing)
    assert cfg["setup_state"]["done"] is True and cfg["setup_state"]["fresh"] is False
    assert (cfg["music_dir"], cfg["yt_playlist_title"], cfg["tg_bot_token"]) == ("E:\\Music", "My game", "123:abc")
    assert setup.on_start() == ""  # once
    assert stored(existing) == cfg


def test_an_install_that_chose_no_music_keeps_none(settings_file):
    settings_file.write_text(json.dumps({"tg_bot_token": "1:x", "music_dir": ""}), encoding="utf-8")
    setup.on_start()
    assert stored(settings_file)["music_dir"] == ""


def test_a_fresh_install_gets_the_wizard_and_none_of_the_old_values(fresh):
    assert setup.on_start() == "fresh"
    cfg = load_settings()
    assert cfg["setup_state"]["done"] is False and cfg["setup_state"]["fresh"] is True
    assert cfg["music_dir"] == "" and cfg["steam_app_id"] == "" and cfg["default_hashtags"] == ""
    assert setup.on_start() == ""


def test_what_a_first_start_writes_by_itself_is_not_content(fresh):
    # a fresh install that has started once on another port: destinations migrated, redirect URIs written
    from app.services import destinations
    destinations.migrate_settings()
    raw = stored(fresh)
    raw.update(vk_redirect_uri="http://127.0.0.1:8814/api/accounts/callback/vk")
    assert not setup.has_content(raw)
    assert setup.has_content({**raw, "modules_disabled": ["vk"]})
    assert setup.has_content({**raw, "languages": ["en"]})


def test_unreadable_settings_are_left_alone(settings_file):
    settings_file.write_text("{not json", encoding="utf-8")
    assert setup.on_start() == ""
    assert settings_file.read_text(encoding="utf-8") == "{not json"


def test_the_server_start_decides_before_the_destinations_are_built(fresh):
    migrate_db()  # what the server and the worker run at start
    cfg = stored(fresh)
    assert cfg["setup_state"]["fresh"] is True and isinstance(cfg["destinations"], list)


def test_an_installs_own_hashtags_and_steam_game_are_used(existing, client, set_settings):
    from app.services import steam_links
    assert load_settings()["setup_state"]["done"]  # the server's start did it
    set_settings(default_hashtags="#mygame #indiedev", steam_app_id="480")
    assert 'value="#mygame #indiedev"' in client.post("/api/hashtags/suggest", data={"text": "x"}).text
    assert steam_links.events_url() == "https://steamcommunity.com/games/480/partnerevents/create"
    assert "games/480/partnerevents" in client.get("/steam").text


def test_a_new_install_suggests_its_own_hashtags(client, set_settings):
    html = client.post("/api/hashtags/suggest", data={"text": "x"}).text
    assert 'value=""' in html and "Settings → Posts" in html
    r = client.post("/api/settings/section/posts", data={"default_hashtags": "indiedev, #gamedev #indiedev"})
    assert r.status_code == 200 and load_settings()["default_hashtags"] == "#indiedev #gamedev"
    assert 'value="#indiedev #gamedev"' in client.post("/api/hashtags/suggest", data={"text": "x"}).text
    assert "/accounts#section_steam" in client.get("/steam").text  # no game set: the button says where


def test_youtube_without_a_default_playlist_adds_none(monkeypatch, make_post, set_settings, session):
    from app.models.models import Job, Post
    from app.platforms import youtube as yt_platform
    from app.services import tokens, youtube_client

    calls = []

    class FakeYT:
        def __init__(self, **kw):
            pass

        def upload_video(self, path, title, description, privacy_status):
            return "vid1", "https://youtu.be/vid1"

        def ensure_playlist(self, name):
            calls.append(("ensure", name))
            return "PL1"

        def add_to_playlist(self, playlist_id, vid):
            calls.append(("add", playlist_id))

    monkeypatch.setattr(youtube_client, "YouTubeClient", FakeYT)
    monkeypatch.setattr(tokens, "yt_access_token", lambda force=False: "a")

    def publish():
        post_id, job_id = make_post("yt", {"media_rel": "v.mp4"})
        post, job = session.get(Post, post_id), session.get(Job, job_id)
        assert yt_platform.publish(session, post, job) is None and post.platform_post_id == "vid1"

    publish()
    assert calls == []  # a new install: no playlist of anyone's
    set_settings(yt_playlist_title="Devlogs")
    publish()
    assert calls == [("ensure", "Devlogs"), ("add", "PL1")]


# ── the wizard ───────────────────────────────────────────────────────────────

def state() -> dict:
    return load_settings()["setup_state"]


def test_a_fresh_install_is_walked_through_setup(fresh, client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/setup"
    page = client.get("/setup").text
    assert "The language you write in" in page
    assert '<option value="en" selected>' in page  # not the built-in ru + en of the install it was written for

    # 1. languages: the starting destinations follow (nothing is set up behind them yet)
    r = client.post("/setup/languages", data={"write": "en", "publish": ["en", "ja"]}, follow_redirects=False)
    assert r.status_code == 303 and state()["step"] == "modules"
    cfg = load_settings()
    assert cfg["languages"] == ["en", "ja"]
    langs = {d["id"]: d["lang"] for d in cfg["destinations"]}
    assert langs["telegram-ru"] == "en" and langs["vk"] == "en" and "facebook-ru" not in langs
    assert set(langs.values()) == {"en"}

    # a restart in the middle comes back to the same step
    assert setup.on_start() == ""
    assert "Where do you publish?" in client.get("/setup").text
    assert client.get("/", follow_redirects=False).status_code == 303

    # 2. networks & features
    client.post("/setup/modules", data={"on": ["telegram", "youtube", "shorts"]})
    assert state()["step"] == "accounts"
    disabled = load_settings()["modules_disabled"]
    assert "telegram" not in disabled and "vk" in disabled and "outreach" in disabled

    # 3. their accounts: the Settings sections themselves, saving in place
    page = client.get("/setup").text
    assert 'id="section_telegram"' in page and 'id="section_youtube"' in page and 'id="section_vk"' not in page
    assert 'id="section_languages"' in page and 'id="ld_langs"' in page  # more channels: the destinations editor
    r = client.post("/api/settings/section/telegram", data={"tg_bot_token": "1:abc", "tg_chat_id_ru": "-100"})
    assert r.status_code == 200 and load_settings()["tg_bot_token"] == "1:abc"
    client.post("/setup/next", data={"step": "accounts"})

    # 4. AI & translation, 5. updates
    assert 'id="section_openai"' in client.get("/setup").text
    client.post("/setup/next", data={"step": "ai"})
    page = client.get("/setup").text
    assert 'id="sf_updates_setup"' in page and 'name="update_mode"' in page
    # the repository is public: no token to ask a new user for (Settings keeps the field, for a private fork)
    assert 'name="update_token"' not in page and 'name="update_repo"' not in page
    client.post("/api/settings/section/updates", data={"_group": "setup", "update_mode": "auto",
                                                       "update_token": "github_pat_x"})
    assert (load_settings()["update_mode"], load_settings()["update_token"]) == ("auto", "")  # only what it shows
    client.post("/setup/next", data={"step": "updates"})

    # done
    assert state()["done"] is True and state()["step"] == "done"
    assert "IndiePulse is set up" in client.get("/setup").text
    assert client.get("/", follow_redirects=False).status_code == 200


def test_every_step_can_be_skipped(fresh, client):
    for step in setup.STEP_IDS[:-1]:
        assert state()["step"] == step
        client.post("/setup/next", data={"step": step})
    assert state()["done"] is True
    assert load_settings()["languages"] == ["ru", "en"] and load_settings()["modules_disabled"] == []


def test_skip_setup_leaves_it_for_good(fresh, client):
    r = client.post("/setup/skip", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    assert state()["done"] is True and client.get("/", follow_redirects=False).status_code == 200


def test_it_is_never_forced_on_an_install_with_settings(existing, client):
    assert client.get("/", follow_redirects=False).status_code == 200
    # it can still be opened, and shows that install's own languages
    assert "IndiePulse is set up" in client.get("/setup").text
    page = client.get("/setup?step=languages").text
    assert 'value="ru" checked' in page and 'value="en" checked' in page and '<option value="ru" selected>' in page
    # Settings → "Run setup again": from the start, but the home page stays the home page
    assert "/setup/restart" in client.get("/accounts").text
    client.post("/setup/restart")
    assert state()["step"] == "languages" and state()["done"] is False and state()["fresh"] is False
    assert client.get("/", follow_redirects=False).status_code == 200
    assert "The language you write in" in client.get("/setup").text


def test_the_languages_step_checks_what_it_gets(fresh, client):
    r = client.post("/setup/languages", data={"write": "", "publish": ["en"]})
    assert r.status_code == 422 and "Pick the language you write in" in r.text
    r = client.post("/setup/languages", data={"write": "en", "other": "eo, Klingon"})
    assert r.status_code == 422 and "“Klingon” is not a language code" in r.text
    assert load_settings()["languages"] == ["ru", "en"] and state()["step"] == "languages"
    client.post("/setup/languages", data={"write": "de", "publish": ["en"], "other": "pt_BR"})
    assert load_settings()["languages"] == ["de", "en", "pt-br"]


def test_a_destination_that_is_set_up_keeps_its_language(existing, client):
    # its Telegram channel is set up and posts in Russian: dropping Russian would send it elsewhere
    r = client.post("/setup/languages", data={"write": "en", "publish": ["en"]})
    assert r.status_code == 422 and "Telegram · RU publishes in Russian" in r.text
    assert load_settings()["languages"] == ["ru", "en"]
    r = client.post("/setup/languages", data={"write": "ru", "publish": ["ru", "en", "ja"]}, follow_redirects=False)
    assert r.status_code == 303 and load_settings()["languages"] == ["ru", "en", "ja"]
    assert {d["id"]: d["lang"] for d in load_settings()["destinations"]}["telegram-ru"] == "ru"
