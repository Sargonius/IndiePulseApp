from __future__ import annotations

import json
import os
from typing import Any, Dict
from loguru import logger

from app.core.config import APP_BASE_URL


SETTINGS_PATH = ".settings.json"


DEFAULTS: Dict[str, Any] = {
	"discord_webhook_url": "",
	"discord_webhook_ru": "",
	"discord_webhook_en": "",
	# Per-file upload limit of the Discord servers (MB): 10 without boosts, 50 at boost
	# level 2, 100 at level 3. Bigger videos are compressed to fit. (Replaced the old
	# "discord_max_upload_mb" = 50, which let 10+ MB uploads hit HTTP 413.)
	"discord_upload_limit_mb": 10,
	# Discord bot token (read-only) — required to collect message reactions for analytics.
	# Webhooks are write-only and cannot read reactions back.
	"discord_bot_token": "",
	# Server whose member count goes to the home screen; empty = the bot's only server
	"discord_guild_id": "",
	# Steamworks "Financial API Group" web API key — daily wishlist + sales reports
	"steam_financial_api_key": "",
	# Secret baked into the "IndiePulse ← Steam" traffic bookmarklet (created on first use)
	"steam_traffic_token": "",
	# The game the store-news export (/steam) opens Steam's event editor for; empty = INDIEPULSE_STEAM_APP_ID from .env.
	# Installs from before it existed got the old built-in one at their first start (app/services/setup.py).
	"steam_app_id": "",
	# UI
	"ui_language": "en",
	# What the post form's "Suggest" puts in the hashtags field ("#a #b"); empty = nothing to suggest
	"default_hashtags": "",
	# The setup wizard (app/services/setup.py): None = not decided yet (the next start does), then
	# {"done", "step", "fresh", "at"}
	"setup_state": None,
	# Languages the posts are written in (Settings → Languages & destinations); the first
	# is the one you write in, the rest are translations (app.services.languages).
	"languages": ["ru", "en"],
	# Every place posts go to, each with its language (app.services.destinations).
	# None = not migrated yet: built from the per-language keys below on first start.
	"destinations": None,
	"vk_client_id": "",
	"vk_client_secret": "",
	"vk_group_id": "",
	"vk_redirect_uri": f"{APP_BASE_URL}/api/accounts/callback/vk",
    "vk_access_token": "",
    "vk_user_access_token": "",
    "vk_user_id": "",
    "x_client_id": "",
    "x_client_secret": "",
    "x_redirect_uri": f"{APP_BASE_URL}/api/accounts/callback/x",
    "x_access_token": "",
    "x_refresh_token": "",
    "x_token_type": "",
    "x_expires_at": 0,
    "x_expires_at_ts": 0,
    "x_code_verifier": "",
    "x_api_key": "",
    "x_api_secret": "",
    "x_oauth_token": "",
    "x_oauth_token_secret": "",
    # Runtime state (not user-editable): when X started answering 402 "credits
    # depleted"; 0 = the last call went through. See services/x_billing.py.
    "x_credits_error_at": 0,
    # YouTube Shorts
    "yt_client_id": "",
    "yt_client_secret": "",
    "yt_access_token": "",
    "yt_refresh_token": "",
    "yt_expires_at": 0,
    # Runtime state for the setup checklist / global alert (not user-editable):
    # last refresh failure that needs a reconnect, when Connect YouTube last worked,
    # the refresh-token expiry Google reported (Testing apps: 7 days), the channel.
    "yt_auth_error": "",
    "yt_auth_error_at": 0,
    "yt_connected_at": 0,
    "yt_refresh_expires_at": 0,
    "yt_channel": "",
    # Optional YouTube Data API key (Google Cloud → Credentials) for creator search on
    # /outreach: unlike the OAuth token it doesn't expire weekly. Empty = use the token.
    "yt_api_key": "",
    # YouTube playlist preferences
    "yt_playlist_id": "",
    # empty = no playlist unless the form picks one (older installs keep theirs: app/services/setup.py)
    "yt_playlist_title": "",
    # Video stats: every 30 min for the first week, then daily until this many days
    # after publish (Shorts keep gathering views for weeks). 0 = first week only.
    "yt_metrics_days": 90,
    # Facebook (two groups)
    "fb_app_id": "",
    "fb_app_secret": "",
    "fb_ru_page_access_token": "",
    "fb_en_page_access_token": "",
    "fb_ru_page_id": "",
    "fb_en_page_id": "",
    # Bluesky (handle + app password, that's it)
    "bluesky_handle": "",
    "bluesky_app_password": "",
    "bluesky_service": "https://bsky.social",
    # Reddit
    "reddit_client_id": "",
    "reddit_client_secret": "",
    "reddit_username": "",
    "reddit_password": "",
    "reddit_subreddit_ru": "",
    "reddit_subreddit_en": "",
    # Telegram
    "tg_bot_token": "",
    "tg_chat_id_ru": "",
    # TikTok — Content Posting API in inbox/draft mode (video.upload scope).
    # tiktok_export_dir is the fallback folder when no token is configured.
    "tiktok_export_dir": "render",
    "tiktok_client_key": "",
    "tiktok_client_secret": "",
    "tiktok_redirect_uri": f"{APP_BASE_URL}/api/accounts/callback/tiktok",
    "tiktok_access_token": "",
    "tiktok_refresh_token": "",
    "tiktok_token_expires_at": 0,
    # what the last connect granted (comma list), whose account it is and when
    # (the /tiktok pipeline adopts factory drafts sent after that moment)
    "tiktok_scope": "",
    "tiktok_open_id": "",
    "tiktok_connected_at": 0,
    # also ask for video.list + user.info.stats + user.info.profile (views, followers,
    # @handle). Off by default: a scope the app hasn't added breaks TikTok's consent page.
    "tiktok_want_stats": False,
    # the country the 🎵 TikTok test aims at (app/services/tiktok_regions.py); "" = not chosen yet
    "tiktok_region": "",
    # Instagram Reels (Graph API; reuses the fb app). ig_user_id = IG business
    # account id; ig_access_token = the linked Facebook Page's access token.
    "ig_user_id": "",
    "ig_access_token": "",
    # One-time OAuth `state` values (CSRF protection for the connect flows)
    "oauth_state_x": "",
    "oauth_state_tiktok": "",
    "oauth_state_youtube": "",
    # AI for translation and outreach letters (app.services.llm): "auto" = OpenAI if it has a key, else Claude
    "llm_provider": "auto",
    "openai_api_key": "",
    "openai_model": "gpt-6-luna",
    "anthropic_api_key": "",
    "anthropic_model": "claude-opus-5-5",
    # DeepL for machine translation (app.services.machine_translate); empty = the free public translators
    "deepl_api_key": "",
    # Updates from GitHub Releases (app/services/updates.py): "off" | "notify" | "auto" (installs when nothing is
    # publishing); the repo; for a private one a fine-grained token, Contents: read-only (redact.py masks *token*)
    "update_mode": "notify",
    "update_repo": "Sargonius/IndiePulseNew",
    "update_token": "",
    # Music folders for the video editors, one per line (Settings → Video & music); empty = none yet. Older
    # installs keep the one they had by default (app/services/setup.py).
    "music_dir": "",
    # Modules turned off on the 🧩 Modules page (app.core.modules); empty = all on
    "modules_disabled": [],
}


def load_settings() -> Dict[str, Any]:
    path = os.path.abspath(SETTINGS_PATH)
    if not os.path.exists(path):
        return DEFAULTS.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            merged = DEFAULTS.copy()
            merged.update(data)
            return merged
    except Exception as exc:
        logger.error(f"load_settings failed: {exc}")
        return DEFAULTS.copy()


def save_settings(partial: Dict[str, Any]) -> Dict[str, Any]:
    # load-merge-write: the web server and the worker (and parallel health checks)
    # all refresh tokens, so without a cross-process lock they'd drop each other's keys.
    from app.core.filelock import FileLock
    with FileLock("settings"):
        return _save_settings_locked(partial)


def _save_settings_locked(partial: Dict[str, Any]) -> Dict[str, Any]:
    path = os.path.abspath(SETTINGS_PATH)
    if os.path.exists(path):
        # load_settings() falls back to DEFAULTS on a parse error — saving on top of
        # that would wipe every stored credential, so refuse instead.
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except Exception as exc:
            logger.error(f"save_settings aborted: {SETTINGS_PATH} is unreadable ({exc}); not overwriting it")
            return load_settings()
    settings = load_settings()
    logger.info(f"save_settings merging keys: {list(partial.keys())}")
    for key, value in partial.items():
        if key in DEFAULTS:
            settings[key] = value
    try:
        # write-then-rename so a crash or a concurrent reader never sees a half-written file
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        for attempt in range(20):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                # Windows: antivirus/indexer or a reader briefly holding the file open
                if attempt == 19:
                    raise
                import time as _time
                _time.sleep(0.05)
        # verify
        with open(path, "r", encoding="utf-8") as f:
            after = json.load(f)
        logger.info(f"save_settings persisted keys: {', '.join(sorted([k for k in partial.keys() if k in after]))}")
    except Exception as exc:
        logger.error(f"save_settings failed: {exc}")
    return settings


