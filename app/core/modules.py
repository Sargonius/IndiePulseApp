"""Modules: parts of IndiePulse a user can turn off (someone without a VK group doesn't need VK at all).

A module that is off disappears: its pages and API answer 404 (`blocked`), its menu items, Settings section and
platform checkboxes are hidden (base.html), the worker doesn't run its periodic jobs and doesn't publish to its
platforms. Nothing is deleted — turned on again, it's all there. The code still lives where it did (routers, services,
platforms); a module says which parts of it are its own.

Which ones are off: the `modules_disabled` list in the settings (the ⚙️ → 🧩 Modules page). A module whose
requirement is off is off too.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Module:
	id: str
	name: str
	description: str
	kind: str                                       # "platform" (publishes somewhere) or "feature"
	platforms: tuple[str, ...] = ()                 # Post.platform values it publishes to
	prefixes: tuple[str, ...] = ()                  # URL prefixes of its own pages and API
	nav: tuple[tuple[str, str, str], ...] = ()      # menu items: (href, label, tooltip)
	section: str = ""                               # its block on the Settings page: id="section_<section>"
	requires: tuple[str, ...] = field(default=())   # other modules it can't work without


def _platform(id: str, name: str, description: str, platforms: tuple[str, ...], api: tuple[str, ...] = (),
			  section: str = "") -> Module:
	oauth = tuple(f"/api/accounts/{step}/{id}" for step in ("connect", "callback"))
	return Module(id, name, description, "platform", platforms, tuple(f"/api/{a}" for a in api) + oauth,
				  section=section or id)


MODULES: list[Module] = [
	_platform("telegram", "Telegram", "Posts to a channel through a bot", ("tg",), ("telegram",)),
	_platform("discord", "Discord", "Posts through channel webhooks; the bot counts members", ("discord",),
			  ("discord",)),
	_platform("vk", "VK", "Posts and videos to a community wall (VK ID login)", ("vk", "vk_video"), ("vk",)),
	_platform("x", "X / Twitter", "Posts with media (OAuth 2; API credits are paid)", ("x",), ("x",)),
	_platform("youtube", "YouTube", "Videos and Shorts, channel stats (Google Cloud project)", ("yt",),
			  ("youtube",)),
	_platform("facebook", "Facebook", "Posts to Pages (a Page per language)", ("fb_ru", "fb_en"), ("facebook",)),
	_platform("instagram", "Instagram Reels", "Reels through the Instagram Graph API", ("ig_reels",)),
	_platform("reddit", "Reddit", "Posts to subreddits (script app)", ("reddit",)),
	_platform("tiktok", "TikTok", "Videos into the TikTok app's drafts: finish them there", ("tiktok",),
			  ("tiktok",)),
	_platform("bluesky", "Bluesky", "Posts with images (app password)", ("bluesky",), ("bluesky",)),
	Module("shorts", "Shorts factory", "A batch of vertical clips: editor, presets, board, reposts", "feature",
		   prefixes=("/shorts", "/api/shorts"), nav=(("/shorts", "🎞️ Shorts", ""),)),
	Module("tiktok_pipeline", "TikTok pipeline", "Proven Shorts → TikTok: checklist, drafts, numbers", "feature",
		   prefixes=("/tiktok",), nav=(("/tiktok", "🎵 TikTok", "TikTok pipeline: proven Shorts → TikTok, "
										"checklist and numbers"),), requires=("tiktok", "shorts")),
	Module("outreach", "Creator outreach", "YouTubers and streamers for your games: search, contacts, keys",
		   "feature", prefixes=("/outreach", "/api/outreach"),
		   nav=(("/outreach", "📣 Outreach", "Creator outreach: YouTubers and streamers"),)),
	Module("steam", "Steam", "Wishlists and sales on the home page, traffic import, store news export", "feature",
		   prefixes=("/steam", "/api/steam"), section="steam"),
	Module("guide", "Marketing guide", "The indie marketing playbook page", "feature", prefixes=("/guide",),
		   nav=(("/guide", "📖", "Indie marketing playbook"),)),
]
BY_ID = {m.id: m for m in MODULES}
_PLATFORM_MODULE = {p: m.id for m in MODULES for p in m.platforms}

SETTINGS_KEY = "modules_disabled"


def resolve_off(disabled) -> frozenset[str]:
	"""The modules turned off plus the ones whose requirement is off (transitively)."""
	off = {m for m in disabled if m in BY_ID}
	changed = True
	while changed:
		changed = False
		for m in MODULES:
			if m.id not in off and any(r in off for r in m.requires):
				off.add(m.id)
				changed = True
	return frozenset(off)


_cache: tuple[tuple, frozenset] | None = None


def off_ids() -> frozenset[str]:
	"""Modules that are off now. Read on every request and in the worker: cached until the settings file changes."""
	global _cache
	from app.services import settings
	path = os.path.abspath(settings.SETTINGS_PATH)
	try:
		key = (path, os.stat(path).st_mtime_ns)
	except OSError:
		key = (path, 0)
	if _cache is None or _cache[0] != key:
		_cache = (key, resolve_off(settings.load_settings().get(SETTINGS_KEY) or ()))
	return _cache[1]


def on(module_id: str) -> bool:
	return module_id not in off_ids()


def platform_on(platform: str) -> bool:
	"""A Post.platform value ('tg', 'fb_en', 'PlatformEnum.yt'...) whose module is on; unknown ones are."""
	key = getattr(platform, "value", None) or str(platform)
	key = key.split(".", 1)[1] if key.startswith("PlatformEnum.") else key
	owner = _PLATFORM_MODULE.get(key)
	return owner is None or on(owner)


def blocked(path: str) -> Module | None:
	"""The module that is off and owns this URL, if any."""
	off = off_ids()
	if not off:
		return None
	for mid in off:
		for prefix in BY_ID[mid].prefixes:
			if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
				return BY_ID[mid]
	return None


def for_templates() -> dict:
	"""What base.html needs: the menu items of the modules that are on, and what to hide on a page."""
	off = off_ids()
	return {
		"nav": {m.id: m.nav for m in MODULES if m.id not in off},
		"off_platforms": sorted(p for m in MODULES if m.id in off for p in m.platforms),
		"off_sections": sorted(BY_ID[m].section for m in off if BY_ID[m].section),
	}
