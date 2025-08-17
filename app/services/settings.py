from __future__ import annotations

import json
import os
from typing import Any, Dict
from loguru import logger


SETTINGS_PATH = ".settings.json"


DEFAULTS: Dict[str, Any] = {
	"discord_webhook_url": "",
	"discord_webhook_ru": "",
	"discord_webhook_en": "",
	"vk_client_id": "",
	"vk_client_secret": "",
	"vk_group_id": "",
	"vk_redirect_uri": "http://127.0.0.1:8000/api/accounts/callback/vk",
    "vk_access_token": "",
    "vk_user_id": "",
    "x_client_id": "",
    "x_client_secret": "",
    "x_redirect_uri": "http://127.0.0.1:8000/api/accounts/callback/x",
    "x_access_token": "",
    "x_refresh_token": "",
    "x_token_type": "",
    "x_expires_at": 0,
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
    # Facebook (two groups)
    "fb_ru_page_access_token": "",
    "fb_en_page_access_token": "",
    "fb_ru_page_id": "",
    "fb_en_page_id": "",
    # Reddit
    "reddit_client_id": "",
    "reddit_client_secret": "",
    "reddit_username": "",
    "reddit_password": "",
    "reddit_subreddit_ru": "",
    "reddit_subreddit_en": "",
    # TikTok (placeholder for export-based workflow)
    "tiktok_export_dir": "render",
    # OpenAI (optional for translation)
    "openai_api_key": "",
    "openai_model": "gpt-4o-mini",
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
    path = os.path.abspath(SETTINGS_PATH)
    settings = load_settings()
    logger.info(f"save_settings merging keys: {list(partial.keys())}")
    for key, value in partial.items():
        if key in DEFAULTS:
            settings[key] = value
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(settings, f, ensure_ascii=False, indent=2)
        # verify
        with open(path, "r", encoding="utf-8") as f:
            after = json.load(f)
        logger.info(f"save_settings persisted keys: {', '.join(sorted([k for k in partial.keys() if k in after]))}")
    except Exception as exc:
        logger.error(f"save_settings failed: {exc}")
    return settings


