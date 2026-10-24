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
import re
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
			updates = {SETTINGS_KEY: {"done": False, "step": STEP_IDS[0], "fresh": True, "at": now}}
		settings._save_settings_locked(updates)
	logger.info(f"setup: first start of this version, {kind} install; wrote {sorted(updates)}")
	return kind


# ── the wizard's steps ───────────────────────────────────────────────────────

STEPS = (
	("languages", "Languages"),
	("modules", "Networks & features"),
	("accounts", "Accounts"),
	("ai", "AI & translation"),
	("updates", "Updates"),
	("done", "Done"),
)
STEP_IDS = [s for s, _title in STEPS]


def _cfg(cfg: Optional[dict]) -> dict:
	if cfg is not None:
		return cfg
	from app.services.settings import load_settings
	return load_settings()


def current(cfg: Optional[dict] = None) -> dict:
	"""The wizard's state, never None: {"done", "step", "fresh", "at"} (step: one of STEP_IDS)."""
	raw = _cfg(cfg).get(SETTINGS_KEY)
	st = dict(raw) if isinstance(raw, dict) else {"done": False, "step": "", "fresh": False, "at": 0}
	if st.get("step") not in STEP_IDS:
		st["step"] = STEP_IDS[0]
	return st


def should_show(cfg: Optional[dict] = None) -> bool:
	"""The home page opens the wizard: an install that started without settings and hasn't finished or skipped
	it. Never one that had settings, nor "Run setup again"."""
	raw = _cfg(cfg).get(SETTINGS_KEY)
	return isinstance(raw, dict) and raw.get("fresh") is True and not raw.get("done")


def _store(**changes) -> dict:
	from app.services.settings import save_settings
	st = {**current(), **changes, "at": int(time.time())}
	save_settings({SETTINGS_KEY: st})
	return st


def after(step: str) -> str:
	i = STEP_IDS.index(step)
	return STEP_IDS[min(i + 1, len(STEP_IDS) - 1)]


def go_to(step: str) -> dict:
	"""The step reached (what a restart comes back to); reaching "done" finishes the wizard."""
	return _store(step=step, done=current()["done"] or step == "done")


def finish() -> dict:
	return _store(done=True)


def restart() -> dict:
	"""Settings → "Run setup again": from the first step, never forced on the home page."""
	return _store(step=STEP_IDS[0], done=False, fresh=False)


# ── step 1: languages ────────────────────────────────────────────────────────

def suggested_languages(cfg: Optional[dict] = None) -> list[str]:
	"""What the first step shows ticked: the languages of the settings — except on a fresh install that still has
	the built-in pair (ru, en: what IndiePulse was written for), where it suggests the language of its interface."""
	from app.services import languages
	from app.services.settings import DEFAULTS
	cfg = _cfg(cfg)
	langs = languages.configured(cfg)
	if current(cfg).get("fresh") and langs == list(DEFAULTS["languages"]):
		return ["en"]
	return langs


def _set_up(dest: dict, cfg: dict) -> bool:
	"""Something of the user's is behind this destination: its account or its target."""
	from app.services import destinations
	kind = destinations.KINDS[dest["module"]]
	resolved = destinations.resolve(dest, cfg)
	return any(str(cfg.get(k) or "").strip() for k in kind.account) or \
		any(str(v or "").strip() for v in resolved["target"].values())


def refit_destinations(langs: list[str], cfg: dict) -> tuple[list[dict], list[str]]:
	"""The destinations for a new list of languages. One in a language that is gone moves to the first language
	when nothing is set up behind it yet (the starting set of a fresh install), or goes when its network already
	has a destination in that language; one that is set up is never changed: it's an error instead."""
	from app.services import destinations, languages
	items, errors = [], []
	stored = destinations.stored(cfg)
	taken = {(d["module"], d["lang"]) for d in stored if d["lang"] in langs}
	for d in stored:
		item = {"id": d["id"], "module": d["module"], "lang": d["lang"], "label": d["label"], "enabled": d["enabled"],
				"skip_without_text": d["skip_without_text"], "target": dict(d["target"])}
		if d["lang"] not in langs:
			if _set_up(d, cfg):
				errors.append(f"{destinations.title(d)} publishes in {languages.name(d['lang'])}: keep that language, "
							  f"or give it another one in Settings → Languages & destinations")
				continue
			if (d["module"], langs[0]) in taken:
				continue
			item["lang"] = langs[0]
			taken.add((d["module"], langs[0]))
		items.append(item)
	return items, errors


def save_languages(write, publish, other="") -> list[str]:
	"""Step 1: the language you write in first, then the others you publish in (ticked, or typed as codes).
	→ errors (nothing saved then)."""
	from app.services import destinations, languages
	from app.services.settings import load_settings
	errors: list[str] = []
	langs: list[str] = []
	typed = [c for c in re.split(r"[\s,;]+", str(other or "")) if c]
	for code in [write, *(publish or []), *typed]:
		c = languages.normalize_code(code)
		if c is None:
			if code:
				errors.append(f"“{code}” is not a language code (two or three letters, like ja or pt-br)")
		elif c not in langs:
			langs.append(c)
	if not languages.normalize_code(write):
		errors.append("Pick the language you write in")
	if errors:
		return errors
	cfg = load_settings()
	items, errors = refit_destinations(langs, cfg)
	if errors:
		return errors
	res = destinations.save(langs, items, cfg)
	return [] if res["ok"] else res["errors"]


# ── step 2: networks & features; step 3: their accounts ──────────────────────

def save_modules(on_ids) -> list[str]:
	"""Step 2: the modules ticked are on, the rest off (the 🧩 Modules page's list)."""
	from app.core import modules
	from app.services.settings import save_settings
	chosen = set(on_ids or ())
	disabled = sorted(m.id for m in modules.MODULES if m.id not in chosen)
	save_settings({modules.SETTINGS_KEY: disabled})
	return disabled


def account_sections(cfg: Optional[dict] = None) -> list[str]:
	"""Step 3: the Settings sections of the modules that are on (networks, and Steam's reports)."""
	from app.core import settings_schema as schema
	return [s.id for s in schema.SECTIONS if s.module and s.shown and schema.on(s)]
