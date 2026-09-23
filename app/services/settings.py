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
	# Discord upload settings
	"discord_max_upload_mb": 50,
	# Discord bot token (read-only) — required to collect message reactions for analytics.
	# Webhooks are write-only and cannot read reactions back.
	"discord_bot_token": "",
	# UI
	"ui_language": "en",
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
    # YouTube Shorts
    "yt_client_id": "",
    "yt_client_secret": "",
    "yt_access_token": "",
    "yt_refresh_token": "",
    "yt_expires_at": 0,
    # YouTube playlist preferences
    "yt_playlist_id": "",
    "yt_playlist_title": "Synthetica",
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
    # Instagram Reels (Graph API; reuses the fb app). ig_user_id = IG business
    # account id; ig_access_token = the linked Facebook Page's access token.
    "ig_user_id": "",
    "ig_access_token": "",
    # One-time OAuth `state` values (CSRF protection for the connect flows)
    "oauth_state_x": "",
    "oauth_state_tiktok": "",
    "oauth_state_youtube": "",
    # OpenAI (optional for translation)
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
    # Local music directory (Windows default path)
    "music_dir": "E:\\SynthSocial\\Music",
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


