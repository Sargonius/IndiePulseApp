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
    from app.core import settings_schema as schema
    updates = schema.BY_ID["updates"]
    token = ("update_token",)
    assert schema.parse_form(updates, {"update_token": ""}, keys=token) == ({}, {})  # empty: the saved one stays
    assert schema.parse_form(updates, {"update_token": "", "update_token__clear": "1"}, keys=token) == \
        ({"update_token": ""}, {})
    # the wizard's group of the section has the mode only: its form never touches a saved token
    set_settings(update_token="github_pat_123456789")
    client.post("/api/settings/section/updates", data={"_group": "setup", "update_mode": "auto",
                                                       "update_token__clear": "1"})
    cfg = load_settings()
    assert (cfg["update_mode"], cfg["update_token"]) == ("auto", "github_pat_123456789")


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


def test_the_tiktok_target_region_is_a_group_of_the_tiktok_section(client, set_settings):
    page = client.get("/accounts").text
    tiktok = page.split('id="section_tiktok"', 1)[1].split('class="settings-section"', 1)[0]
    assert 'id="sf_tiktok_target"' in tiktok and '<option value="JP"' in tiktok
    r = client.post("/api/settings/section/tiktok", data={"_group": "target", "tiktok_region": "DE"})
    assert r.status_code == 200 and load_settings()["tiktok_region"] == "DE"
    assert client.get("/setup?step=accounts").text.count('id="sf_tiktok_target"') == 1   # the wizard has it too
    # the pipeline turned off: the TikTok platform's section stays, without the test's region
    set_settings(modules_disabled=["tiktok_pipeline"])
    page = client.get("/accounts").text
    assert 'id="section_tiktok"' in page and 'id="sf_tiktok_target"' not in page


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


def test_a_section_save_brings_a_target_to_a_destination(client, set_settings):
    # like the old /api/settings/save (app.services.destinations.save_keys): a chat id / webhook / subreddit set
    # up later through its section reaches a destination, even one removed in the editor
    from app.services import destinations
    items = [dict(d, module=g["module"]) for g in destinations.editor_context()["groups"] for d in g["destinations"]
             if d["id"] != "telegram-ru"]
    assert destinations.save(["ru", "en"], items)["ok"] and destinations.get("telegram-ru") is None
    client.post("/api/settings/section/telegram", data={"tg_bot_token": "1:abc", "tg_chat_id_ru": "-100500"})
    dest = destinations.get("telegram-ru")
    assert dest["target"] == {"chat_id": "-100500"} and destinations.ready(dest)
    client.post("/api/settings/section/discord", data={"discord_webhook_url": "https://hook/one"})
    assert destinations.get("discord")["target"] == {"webhook": "https://hook/one"}
