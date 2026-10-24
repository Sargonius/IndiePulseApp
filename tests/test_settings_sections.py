"""Settings sections made from their schemas: one save per section, checked, secrets kept, only its own keys."""
import json

from app.services.settings import load_settings


def saved(settings_file) -> dict:
    return json.loads(settings_file.read_text(encoding="utf-8"))


def test_a_section_saves_its_fields_and_shows_secrets_as_a_hint(client, settings_file):
    r = client.post("/api/settings/section/telegram", data={"tg_bot_token": "123456:SECRET-TOKEN",
                                                            "tg_chat_id_ru": "-1001234567890"})
    assert r.status_code == 200 and "Saved ✓" in r.text
    assert "SECRET-TOKEN" not in r.text and "•••OKEN" in r.text and 'value="-1001234567890"' in r.text
    cfg = load_settings()
    assert (cfg["tg_bot_token"], cfg["tg_chat_id_ru"]) == ("123456:SECRET-TOKEN", "-1001234567890")
    # the secret left empty stays; a text field emptied is emptied
    client.post("/api/settings/section/telegram", data={"tg_bot_token": "", "tg_chat_id_ru": ""})
    cfg = load_settings()
    assert (cfg["tg_bot_token"], cfg["tg_chat_id_ru"]) == ("123456:SECRET-TOKEN", "")


def test_a_section_writes_only_its_own_keys(client, set_settings, settings_file):
    set_settings(x_client_id="mine", tg_bot_token="1:old")
    client.post("/api/settings/section/telegram", data={"tg_chat_id_ru": "@mychannel", "x_client_id": "forged",
                                                        "x_access_token": "forged", "music_dir": "C:\\x"})
    cfg = saved(settings_file)
    assert cfg["tg_chat_id_ru"] == "@mychannel" and cfg["x_client_id"] == "mine"
    assert cfg.get("x_access_token", "") == "" and cfg.get("music_dir", "") != "C:\\x"


def test_what_does_not_pass_its_check_saves_nothing(client, set_settings):
    set_settings(tg_chat_id_ru="-100", tg_bot_token="1:old")
    r = client.post("/api/settings/section/telegram", data={"tg_bot_token": "2:new", "tg_chat_id_ru": "t.me/chan"})
    assert r.status_code == 422 and "Not saved" in r.text and "a @channel name" in r.text
    assert 'value="t.me/chan"' in r.text                          # what was typed stays, to be fixed
    assert "2:new" not in r.text                                  # a secret never comes back
    cfg = load_settings()
    assert (cfg["tg_bot_token"], cfg["tg_chat_id_ru"]) == ("1:old", "-100")
    r = client.post("/api/settings/section/openai", data={"llm_provider": "gpt"})
    assert r.status_code == 422 and load_settings()["llm_provider"] == "auto"
    r = client.post("/api/settings/section/discord", data={"discord_webhook_url": "discord.com/api/webhooks/1"})
    assert r.status_code == 422 and "https://" in r.text


def test_numbers_clamp_and_empty_means_the_default(client):
    client.post("/api/settings/section/video", data={"music_dir": ' E:\\Music \n"D:\\Shorts"',
                                                     "discord_upload_limit_mb": "9999"})
    cfg = load_settings()
    assert cfg["music_dir"] == "E:\\Music\nD:\\Shorts" and cfg["discord_upload_limit_mb"] == 500
    client.post("/api/settings/section/bluesky", data={"bluesky_handle": "@me.bsky.social", "bluesky_service": ""})
    cfg = load_settings()
    assert cfg["bluesky_handle"] == "me.bsky.social" and cfg["bluesky_service"] == "https://bsky.social"


def test_a_field_of_a_module_that_is_off_is_neither_shown_nor_saved(client, set_settings):
    set_settings(modules_disabled=["discord"], discord_upload_limit_mb=50)
    page = client.get("/accounts").text
    video = page.split('id="section_video"', 1)[1].split('class="settings-section"', 1)[0]
    assert "music_dir" in video and "discord_upload_limit_mb" not in video
    client.post("/api/settings/section/video", data={"music_dir": "", "discord_upload_limit_mb": "10"})
    assert load_settings()["discord_upload_limit_mb"] == 50


def test_a_secret_with_a_remove_box_can_be_cleared(client, set_settings):
    set_settings(update_token="github_pat_123456789")
    client.post("/api/settings/section/updates", data={"_group": "setup", "update_mode": "auto"})
    cfg = load_settings()
    assert (cfg["update_mode"], cfg["update_token"]) == ("auto", "github_pat_123456789")
    client.post("/api/settings/section/updates", data={"_group": "setup", "update_mode": "auto",
                                                       "update_token__clear": "1"})
    assert load_settings()["update_token"] == ""


def test_a_group_of_a_hand_written_section(client, set_settings):
    set_settings(yt_client_id="cid", yt_playlist_title="Old")
    r = client.post("/api/settings/section/youtube", data={"_group": "opts", "yt_metrics_days": "30",
                                                           "yt_playlist_id": "", "yt_playlist_title": "",
                                                           "yt_client_id": "forged"})
    assert r.status_code == 200 and 'id="sf_youtube_opts"' in r.text
    cfg = load_settings()
    assert (cfg["yt_metrics_days"], cfg["yt_playlist_title"], cfg["yt_client_id"]) == (30, "", "cid")
    # its other fields are the hand-written forms' to save
    assert client.post("/api/settings/section/youtube", data={"yt_client_id": "x"}).status_code == 404
    assert client.post("/api/settings/section/youtube", data={"_group": "nope"}).status_code == 404


def test_unknown_sections_and_sections_without_a_form(client):
    assert client.post("/api/settings/section/nope", data={}).status_code == 404
    assert client.post("/api/settings/section/modules", data={"modules_disabled": "vk"}).status_code == 404
    assert load_settings()["modules_disabled"] == []


def test_the_settings_page_renders_every_section(client, set_settings):
    from app.core import settings_schema
    set_settings(steam_financial_api_key="steamkey1234", tg_bot_token="123:abcdWXYZ")
    page = client.get("/accounts").text
    for s in settings_schema.SECTIONS:
        if s.shown:
            assert f'id="section_{s.id}"' in page or (s.id == "updates" and 'id="update_panel"' in page), s.id
    assert 'hx-post="/api/settings/section/telegram"' in page and "•••WXYZ" in page and "123:abcdWXYZ" not in page
    assert "✓ key •••1234" in page                                 # Steam's badge


def test_the_old_save_keeps_working_and_comes_back_where_it_was(client):
    r = client.post("/api/settings/save", data={"vk_group_id": "123", "x_client_id": ""})
    assert r.headers["HX-Redirect"] == "/accounts" and load_settings()["vk_group_id"] == "123"
    r = client.post("/api/settings/save", data={"vk_group_id": "456"},
                    headers={"HX-Current-URL": "http://127.0.0.1:8000/setup?step=accounts"})
    assert r.headers["HX-Redirect"] == "/setup?step=accounts" and load_settings()["vk_group_id"] == "456"
