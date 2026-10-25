"""Settings schemas (app/core/settings_schema.py): every setting has one owner, and a form field is checked."""
import re

import pytest

from app.core import modules
from app.core import settings_schema as schema
from app.services.settings import DEFAULTS


def test_every_setting_belongs_to_exactly_one_section():
    owners: dict = {}
    for s in schema.SECTIONS:
        for f in s.fields:
            owners.setdefault(f.key, []).append(s.id)
    assert sorted(k for k in DEFAULTS if k not in owners) == []           # a key nobody declares
    assert sorted(k for k in owners if k not in DEFAULTS) == []           # a declared key save_settings drops
    assert {k: v for k, v in owners.items() if len(v) > 1} == {}         # a key with two owners


def test_a_module_section_is_the_block_the_module_names():
    for s in schema.SECTIONS:
        assert s.id == s.id.strip() and re.fullmatch(r"[a-z_]+", s.id)
        if s.module:
            assert s.module in modules.BY_ID
            if s.shown:
                assert modules.BY_ID[s.module].section == s.id
    for m in modules.MODULES:
        if m.section:
            assert schema.BY_ID[m.section].module == m.id


RUNTIME = re.compile(r"^oauth_state_|_expires_at|_error_at$|_connected_at$|_code_verifier$")
OAUTH_TOKENS = ("x_access_token", "x_refresh_token", "yt_access_token", "yt_refresh_token", "tiktok_access_token",
                "tiktok_refresh_token")


def test_runtime_state_is_not_user_editable():
    for key, f in schema.FIELDS.items():
        if RUNTIME.search(key) or key in OAUTH_TOKENS:
            assert f.runtime and not f.editable, key
        if f.runtime:
            assert not f.form
    # and what the user types in is: the keys a connect flow can't fill
    for key in ("tg_bot_token", "vk_access_token", "x_client_secret", "update_token", "music_dir"):
        assert schema.FIELDS[key].editable


def test_fields_are_well_formed():
    for key, f in schema.FIELDS.items():
        assert f.type in schema.TYPES, key
        if f.editable:
            assert f.label, key
        if f.type == "select":
            assert f.choices and DEFAULTS[key] in {c[0] for c in f.choices}, key
        if f.type == "number":
            assert isinstance(DEFAULTS[key], int) and f.min <= DEFAULTS[key] <= f.max, key
        if f.clearable:
            assert f.type == "secret"
        if f.module:
            assert f.module in modules.BY_ID
    for s in schema.SECTIONS:
        for name, keys in s.groups:
            assert s.custom and keys and all(schema.OWNER[k] == s.id and schema.FIELDS[k].editable for k in keys)


@pytest.mark.parametrize("key,raw,expected", [
    ("yt_metrics_days", "45", 45),
    ("yt_metrics_days", "99999", 3650),       # clamped, as the old form did
    ("yt_metrics_days", "", 90),              # empty: the default
    ("discord_upload_limit_mb", "50.0", 50),
    ("bluesky_handle", " @me.bsky.social ", "me.bsky.social"),
    ("bluesky_service", "", "https://bsky.social"),
    ("reddit_subreddit_en", "r/gamedev", "gamedev"),
    ("music_dir", ' E:\\Music \n\n"D:\\Shorts";F:\\X ', "E:\\Music\nD:\\Shorts\nF:\\X"),
    ("update_repo", "https://github.com/me/Other.git", "me/Other"),
    ("llm_provider", "anthropic", "anthropic"),
    ("tg_chat_id_ru", "-1001234567890", "-1001234567890"),
    ("tg_chat_id_ru", "@mychannel", "@mychannel"),
    ("discord_guild_id", "", ""),             # empty: cleared
    ("tiktok_region", "JP", "JP"),
    ("tiktok_region", "", ""),                # not chosen (yet): never Japan by default
])
def test_a_field_cleans_what_it_gets(key, raw, expected):
    assert schema.parse_field(schema.FIELDS[key], raw) == (expected, "")


@pytest.mark.parametrize("key,raw", [
    ("yt_metrics_days", "abc"),
    ("llm_provider", "gpt"),
    ("discord_webhook_url", "discord.com/api/webhooks/1"),
    ("tg_chat_id_ru", "t.me/mychannel"),
    ("update_repo", "nope"),
    ("ig_user_id", "my account"),
    ("tiktok_region", "Mars"),
])
def test_a_field_refuses_what_it_cannot_take(key, raw):
    value, error = schema.parse_field(schema.FIELDS[key], raw)
    assert value is schema.KEEP and error


def test_a_secret_left_empty_keeps_the_saved_one():
    f = schema.FIELDS["tg_bot_token"]
    assert schema.parse_field(f, "") == (schema.KEEP, "")
    assert schema.parse_field(f, " 123:abc ") == ("123:abc", "")
    token = schema.FIELDS["update_token"]
    assert schema.parse_field(token, "", clear=True) == ("", "")     # its "remove the saved one" box
    assert schema.parse_field(f, "", clear=True) == (schema.KEEP, "")  # a secret without that box


def test_the_old_save_takes_only_what_is_filled_in():
    form = {"tg_chat_id_ru": "", "tg_bot_token": "1:x", "yt_metrics_days": "abc", "x_access_token": "forged",
            "nope": "1", "modules_disabled": "vk", "vk_group_id": " 123 "}
    assert schema.parse_legacy(form) == {"tg_bot_token": "1:x", "vk_group_id": "123"}


def test_the_form_shows_a_hint_never_the_secret():
    s = schema.BY_ID["telegram"]
    view = schema.form_view(s, {**DEFAULTS, "tg_bot_token": "123456:ABCDEFG", "tg_chat_id_ru": "-100"})
    by_key = {v["f"].key: v for v in view["fields"]}
    assert by_key["tg_bot_token"]["value"] == "" and by_key["tg_bot_token"]["hint"] == "•••DEFG"
    assert by_key["tg_chat_id_ru"]["value"] == "-100"
    assert [a.label for a in view["actions"]] == ["Test", "Check"]
