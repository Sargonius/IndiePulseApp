"""First run: a fresh install gets the setup wizard; an install with settings never does, and keeps what it had
from the built-in defaults that are gone now (app/services/setup.py)."""
import json

import pytest

from app.db.session import migrate_db
from app.services import setup
from app.services.settings import DEFAULTS, load_settings
from tests.conftest import FRESH_SETTINGS

# what the settings of the install IndiePulse was written for look like, before this version
OWNER = {"tg_bot_token": "123:abc", "tg_chat_id_ru": "-100", "music_dir": "E:\\SynthSocial\\Music",
         "yt_playlist_title": "Synthetica", "languages": ["ru", "en"], "update_mode": "notify"}


@pytest.fixture(autouse=True)
def no_steam_env(monkeypatch):
    """STEAM_APP_ID from this computer's environment would stand in for the built-in default."""
    from app.core import config
    monkeypatch.setattr(config, "STEAM_APP_ID", "")


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
    for key, value in DEFAULTS.items():
        text = json.dumps(value)
        for personal in ("Synthetica", "SynthSocial", "2847510", "Project Apocalypse"):
            assert personal not in text, key
    assert DEFAULTS["music_dir"] == DEFAULTS["yt_playlist_title"] == DEFAULTS["steam_app_id"] == ""
    assert DEFAULTS["default_hashtags"] == ""


def test_an_existing_install_is_set_up_and_keeps_its_old_defaults(existing):
    assert setup.on_start() == "existing"
    cfg = stored(existing)
    assert cfg["setup_state"]["done"] is True and cfg["setup_state"]["fresh"] is False
    assert cfg["steam_app_id"] == "2847510"
    assert cfg["default_hashtags"] == "#Synthetica #UnrealEngine #Tycoon #Strategy #Gamedev #Indie"
    # what its settings held stays as it was
    assert (cfg["music_dir"], cfg["yt_playlist_title"], cfg["tg_bot_token"]) == \
        ("E:\\SynthSocial\\Music", "Synthetica", "123:abc")
    assert setup.on_start() == ""  # once
    assert stored(existing) == cfg


def test_an_old_default_it_never_saved_is_written_down(settings_file, monkeypatch):
    from app.core import config
    monkeypatch.setattr(config, "STEAM_APP_ID", "1156500")  # set in its .env
    settings_file.write_text(json.dumps({"tg_bot_token": "1:x", "yt_playlist_title": "",
                                         "default_hashtags": "#mine"}), encoding="utf-8")
    assert setup.on_start() == "existing"
    cfg = stored(settings_file)
    # the old code fell back to these when the settings had none
    assert cfg["music_dir"] == "E:\\SynthSocial\\Music" and cfg["yt_playlist_title"] == "Synthetica"
    assert cfg["steam_app_id"] == "1156500" and cfg["default_hashtags"] == "#mine"


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


def test_the_owners_hashtags_playlist_and_steam_game_still_work(existing, client):
    from app.services import steam_links
    assert load_settings()["setup_state"]["done"]  # the server's start did it
    html = client.post("/api/hashtags/suggest", data={"text": "x"}).text
    assert 'value="#Synthetica #UnrealEngine #Tycoon #Strategy #Gamedev #Indie"' in html
    assert steam_links.events_url() == "https://steamcommunity.com/games/2847510/partnerevents/create"
    assert "games/2847510/partnerevents" in client.get("/steam").text


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
