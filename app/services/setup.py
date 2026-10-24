"""First run: a fresh install gets the setup wizard (/setup), an install that already has settings never does.

What decides it is the first start of this version (migrate_db calls on_start before anything else writes the
settings): settings with content → an existing install: its setup is marked done, and the values it used to get
from code defaults that are gone now (those of the install IndiePulse was first written for: a music folder, a
playlist, a Steam game, hashtags) are written into its settings, so nothing changes for it. No settings → a fresh install: the wizard is
pending, and the home page opens it until it is finished or skipped.

The wizard's progress is a settings key too (`setup_state`), so a restart in the middle resumes where it was:
	{"done": false, "step": "modules", "fresh": true, "at": 1790000000}
`fresh` = this install started without settings; only then does the home page send you to the wizard ("Run setup
again" from Settings never forces it on anyone).
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from loguru import logger

SETTINGS_KEY = "setup_state"

# What these settings were before they had neutral defaults; existing installs keep them (legacy_values).
LEGACY_MUSIC_DIR = "E:\\SynthSocial\\Music"          # settings.DEFAULTS["music_dir"]
LEGACY_PLAYLIST = "Synthetica"                        # DEFAULTS["yt_playlist_title"] and youtube.py's fallback
LEGACY_STEAM_APP_ID = "2847510"                       # config.STEAM_APP_ID's default
LEGACY_HASHTAGS = "#Synthetica #UnrealEngine #Tycoon #Strategy #Gamedev #Indie"  # api/content.py suggest_hashtags

# Written by IndiePulse itself on a first start, or derived from the address it runs on: not a sign of a user.
_NOT_CONTENT = {"destinations", SETTINGS_KEY, "vk_redirect_uri", "x_redirect_uri", "tiktok_redirect_uri"}


def has_content(raw: dict) -> bool:
	"""Settings someone has used: a value of theirs that isn't empty and isn't the default."""
	from app.services.settings import DEFAULTS
	for key, value in raw.items():
		if key not in DEFAULTS or key in _NOT_CONTENT:
			continue
		if value in (None, "", 0, False, [], {}) or value == DEFAULTS[key]:
			continue
		return True
	return False


def legacy_values(raw: dict) -> dict:
	"""For an existing install: the old code defaults it relied on, where its settings don't say otherwise."""
	from app.core import config
	out = {}
	if "music_dir" not in raw:  # an empty one was set on purpose: no music
		out["music_dir"] = LEGACY_MUSIC_DIR
	if not str(raw.get("yt_playlist_title") or "").strip():  # empty fell back to it too
		out["yt_playlist_title"] = LEGACY_PLAYLIST
	if not str(raw.get("steam_app_id") or "").strip():
		out["steam_app_id"] = str(config.STEAM_APP_ID or LEGACY_STEAM_APP_ID).strip()
	if not str(raw.get("default_hashtags") or "").strip():
		out["default_hashtags"] = LEGACY_HASHTAGS
	return out


def _read_raw() -> Optional[dict]:
	"""The settings file as stored ({} when there is none); None when it can't be read."""
	from app.services import settings
	path = os.path.abspath(settings.SETTINGS_PATH)
	if not os.path.exists(path):
		return {}
	try:
		with open(path, "r", encoding="utf-8") as f:
			data = json.load(f)
	except Exception as exc:
		logger.error(f"setup: the settings file can't be read ({exc}); deciding on the next start")
		return None
	return data if isinstance(data, dict) else None


def on_start() -> str:
	"""Once per install, on the first start of this version: "existing" / "fresh"; "" = decided before (or the
	settings can't be read: the next start tries again). Inside the settings lock, so the server and the worker,
	starting together, decide once."""
	from app.core.filelock import FileLock
	from app.services import settings
	with FileLock("settings"):
		raw = _read_raw()
		if raw is None or isinstance(raw.get(SETTINGS_KEY), dict):
			return ""
		now = int(time.time())
		if has_content(raw):
			kind = "existing"
			updates = {SETTINGS_KEY: {"done": True, "step": "done", "fresh": False, "at": now}, **legacy_values(raw)}
		else:
			kind = "fresh"
			updates = {SETTINGS_KEY: {"done": False, "step": "", "fresh": True, "at": now}}
		settings._save_settings_locked(updates)
	logger.info(f"setup: first start of this version, {kind} install; wrote {sorted(updates)}")
	return kind
