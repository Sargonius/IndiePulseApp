"""What each setting is: who owns it, how the Settings page and the setup wizard show it, how a save checks it.

Every key of app.services.settings.DEFAULTS belongs to exactly one section (tests/test_settings_schema.py checks
it): a module's (app.core.modules — same id, and the Settings block id="section_<id>" the module already names)
or an app-wide one (Languages & destinations, AI & translation, Video & music, Updates…). A key is one of three
things:

- edited by its section's form (the default): rendered by partials/settings_form.html and saved by
  POST /api/settings/section/<id>, which checks it against its Field first;
- the user's, but edited by an editor of its own (`form=False`, `where` says which): languages and destinations,
  🧩 Modules, the outreach page's YouTube key, the TikTok pipeline's stats switch;
- runtime state (`runtime=True`): what IndiePulse writes itself — tokens from an OAuth connect, expiry times,
  error stamps, oauth_state_* — never in a form.

A section is either rendered from its fields or hand-written (`custom`: the OAuth connect flows of YouTube, X,
TikTok, VK and Facebook, the destinations editor, the Updates panel); both sit in the same frame
(partials/settings_section.html) and the hand-written ones still declare their keys here.

Kept apart from modules.py, which is read on every request (which URLs are off) and knows nothing about settings
keys; this one is read by the Settings page, the wizard and their saves.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable, Optional

TYPES = ("text", "secret", "number", "select", "textarea", "bool", "url")


@dataclass(frozen=True)
class Field:
	key: str
	label: str = ""
	type: str = "text"
	help: str = ""                  # next to the label; written here (may hold a link), never user input
	placeholder: str = ""
	choices: tuple = ()             # select: (value, label) pairs
	min: Optional[int] = None       # number: a value outside min..max is clamped (what the old form did)
	max: Optional[int] = None
	pattern: str = ""               # text / url: the cleaned value must match it (when not empty)
	pattern_error: str = ""
	required: bool = False
	blank: str = ""                 # an empty field: "" = stored empty, "default" = back to the default
	clean: Optional[Callable[[str], str]] = None    # tidies the value before it is checked
	display: Optional[Callable[[dict], Any]] = None  # what the form shows instead of the stored value
	heading: str = ""               # a sub-heading above it
	below: str = ""                 # a template under the input (the music folders found)
	module: str = ""                # in an app-wide section: shown only while this module is on
	clearable: bool = False         # a secret with a "remove the saved one" checkbox
	rows: int = 3                   # textarea
	form: bool = True               # False: the user's, edited elsewhere (`where`)
	runtime: bool = False           # written by IndiePulse itself; never in a form
	where: str = ""

	@property
	def editable(self) -> bool:
		return self.form and not self.runtime


def state(key: str, what: str = "") -> Field:
	"""Runtime state: written by IndiePulse (a connect flow, a refresh, an error stamp), not by the user."""
	return Field(key, label=what, form=False, runtime=True, where=what)


def elsewhere(key: str, where: str, label: str = "", type: str = "text") -> Field:
	"""The user's setting, edited by an editor of its own instead of its section's form."""
	return Field(key, label=label, type=type, form=False, where=where)


@dataclass(frozen=True)
class Action:
	"""A button next to Save: Test, Check… (htmx; its answer goes to the form's status line)."""
	label: str
	url: str
	method: str = "post"
	title: str = ""
	include: str = ""   # a field key whose current value goes along (Test a model before saving it)


@dataclass(frozen=True)
class Section:
	id: str                         # id="section_<id>"; a module's section has the module's id
	title: str
	fields: tuple = ()
	module: str = ""                # the 🧩 module it belongs to; "" = app-wide, always there
	custom: str = ""                # a hand-written body (template) instead of the form from the fields
	frame: bool = True              # False: the custom template draws its own frame (Updates, loaded by htmx)
	wide: bool = False              # a wider left column (the destinations editor)
	intro: str = ""                 # a note above the form
	actions: tuple = ()
	groups: tuple = ()              # forms of a hand-written section made from its fields: ((name, (key, …)), …)
	status: bool = False           # its badge comes from /api/settings/status (static/settings.js fills it in)
	badge: Optional[Callable[[dict], tuple]] = None   # else this: cfg → (text, css class) or None
	context: Optional[Callable[[dict], dict]] = None  # extra values for its templates (the music folders found)

	@property
	def form_fields(self) -> tuple:
		return tuple(f for f in self.fields if f.editable)

	@property
	def shown(self) -> bool:
		"""On the Settings page: something to edit there (a section of runtime state only isn't)."""
		return bool(self.custom or self.form_fields)


# ── helpers for the declarations ─────────────────────────────────────────────

def _hint(value) -> str:
	value = str(value or "")
	return ("•••" + value[-4:]) if value else ""


def _saved_badge(key: str, word: str) -> Callable[[dict], Optional[tuple]]:
	def badge(cfg: dict) -> Optional[tuple]:
		hint = _hint(cfg.get(key))
		return (f"✓ {word} {hint}", "configured") if hint else None
	return badge


def _lstrip_at(v: str) -> str:
	return v.lstrip("@")


def _subreddit(v: str) -> str:
	return re.sub(r"^/?r/", "", v.strip()).strip("/")


def _music_dirs(v: str) -> str:
	dirs = [d.strip().strip('"') for d in v.replace(";", "\n").splitlines()]
	return "\n".join(d for d in dirs if d)


def _music_display(cfg: dict) -> str:
	from app.services import music_library
	return "\n".join(music_library.folders(cfg))


def _music_context(cfg: dict) -> dict:
	from app.services import music_library
	return {"music_folders": music_library.folder_rows(cfg)}


def _github_repo(v: str) -> str:
	repo = v.strip().strip("/")
	if repo.startswith("https://github.com/"):  # pasted from the address bar
		repo = repo[len("https://github.com/"):].removesuffix(".git").strip("/")
	return repo


def _instagram_badge(cfg: dict) -> tuple:
	ok = bool(str(cfg.get("ig_user_id") or "").strip() and str(cfg.get("ig_access_token") or "").strip())
	return ("✓", "") if ok else ("—", "")


_DESTINATIONS = "Settings → Languages & destinations"
_TO_DESTINATIONS = "<a href=\"#section_languages\">Languages &amp; destinations</a>"


# ── the sections, in the order of the Settings page ──────────────────────────

SECTIONS: list[Section] = [
	Section("languages", "Languages & destinations", custom="partials/settings/languages.html", wide=True, fields=(
		elsewhere("languages", _DESTINATIONS, "Languages"),
		elsewhere("destinations", _DESTINATIONS, "Destinations"),
		elsewhere("ui_language", "nowhere yet: the interface is in English", "Interface language"),
	)),
	Section("discord", "Discord", module="discord", status=True, actions=(
		Action("Test", "/api/discord/test"),
	), fields=(
		Field("discord_webhook_url", "Webhook URL", "url", help=f"a webhook per language: {_TO_DESTINATIONS}",
			  placeholder="https://discord.com/api/webhooks/..."),
		Field("discord_bot_token", "Bot Token", "secret", help="collects reactions for analytics",
			  placeholder="Bot token from the Discord Developer Portal"),
		Field("discord_guild_id", "Server ID", help="for the member count on Home; leave empty if the bot is in one "
			  "server", placeholder="Server ID (right-click the server → Copy Server ID)", pattern=r"\d+",
			  pattern_error="A server ID is a number"),
		elsewhere("discord_webhook_ru", _DESTINATIONS, "Webhook (RU)", "url"),
		elsewhere("discord_webhook_en", _DESTINATIONS, "Webhook (EN)", "url"),
	)),
	Section("steam", "Steam — reports", module="steam", badge=_saved_badge("steam_financial_api_key", "key"), actions=(
		Action("Check", "/api/steam/financial/test"),
	), fields=(
		Field("steam_financial_api_key", "Financial API key", "secret",
			  help="daily wishlists and sales, broken down by country",
			  placeholder="Web API key of the Financial API group"),
		state("steam_traffic_token", "the secret of the “IndiePulse ← Steam” bookmarklet, made on first use"),
	)),
	Section("telegram", "Telegram", module="telegram", status=True, actions=(
		Action("Test", "/api/telegram/test"),
		Action("Check", "/api/telegram/check"),
	), fields=(
		Field("tg_bot_token", "Bot Token", "secret", placeholder="123456:ABC-DEF..."),
		Field("tg_chat_id_ru", "Chat ID (group/channel)", help=f"channels in other languages: {_TO_DESTINATIONS}",
			  placeholder="-1001234567890", pattern=r"-?\d+|@\w{4,}",
			  pattern_error="A chat ID is a number (-100…) or a @channel name"),
	)),
	Section("vk", "VK", module="vk", custom="partials/settings/vk.html", status=True, fields=(
		Field("vk_client_id", "Client ID"),
		Field("vk_client_secret", "Client Secret", "secret"),
		Field("vk_group_id", "Group ID", placeholder="123456789"),
		Field("vk_redirect_uri", "Redirect URI", "url"),
		Field("vk_access_token", "Access Token", "secret"),
		Field("vk_user_access_token", "User Access Token (for media upload)", "secret"),
		state("vk_user_id", "the VK user of the last connect"),
	)),
	Section("x", "X / Twitter", module="x", custom="partials/settings/x.html", status=True, fields=(
		Field("x_client_id", "Client ID (OAuth 2.0)"),
		Field("x_client_secret", "Client Secret (OAuth 2.0)", "secret"),
		Field("x_redirect_uri", "Redirect URI", "url"),
		Field("x_api_key", "API Key / Consumer Key", heading="OAuth 1.0a (for media upload)"),
		Field("x_api_secret", "API Secret / Consumer Secret", "secret"),
		Field("x_oauth_token", "OAuth1 Access Token", "secret"),
		Field("x_oauth_token_secret", "OAuth1 Access Token Secret", "secret"),
		state("x_access_token", "Authorize"),
		state("x_refresh_token", "Authorize"),
		state("x_token_type", "Authorize"),
		state("x_expires_at", "Authorize: the token's lifetime, s"),
		state("x_expires_at_ts", "Authorize: when the token expires"),
		state("x_code_verifier", "Authorize: the PKCE verifier of the flow under way"),
		state("x_credits_error_at", "when X started answering 402 “credits depleted”; 0 = the last call went through"),
		state("oauth_state_x", "the one-time state of the connect flow"),
	)),
	Section("youtube", "YouTube", module="youtube", custom="partials/settings/youtube.html", status=True,
			groups=(("opts", ("yt_metrics_days", "yt_playlist_id", "yt_playlist_title")),), fields=(
		Field("yt_client_id", "Client ID"),
		Field("yt_client_secret", "Client Secret", "secret"),
		Field("yt_metrics_days", "Video stats, days after publishing", "number", min=0, max=3650, blank="default",
			  help="first week every 30 min, then daily; 0 = first week only"),
		Field("yt_playlist_id", "Default playlist ID", help="videos go into it unless the form picks another"),
		Field("yt_playlist_title", "Default playlist name", help="made when it doesn't exist; used when the ID is "
			  "empty"),
		state("yt_access_token", "Connect YouTube (the section also takes a pasted one)"),
		state("yt_refresh_token", "Connect YouTube (the section also takes a pasted one)"),
		state("yt_expires_at", "Connect YouTube: when the access token expires"),
		state("yt_auth_error", "the last refresh failure that needs a reconnect"),
		state("yt_auth_error_at", "when that failure happened"),
		state("yt_connected_at", "when Connect YouTube last worked"),
		state("yt_refresh_expires_at", "the refresh-token expiry Google reported (Testing apps: 7 days)"),
		state("yt_channel", "the connected channel"),
		state("oauth_state_youtube", "the one-time state of the connect flow"),
	)),
	Section("bluesky", "Bluesky", module="bluesky", status=True, actions=(
		Action("Test", "/api/bluesky/test"),
	), fields=(
		Field("bluesky_handle", "Handle", placeholder="yourname.bsky.social", clean=_lstrip_at),
		Field("bluesky_app_password", "App Password", "secret", placeholder="xxxx-xxxx-xxxx-xxxx"),
		Field("bluesky_service", "Service URL", "url", help="optional, for a self-hosted PDS", blank="default",
			  placeholder="https://bsky.social"),
	)),
	Section("facebook", "Facebook", module="facebook", custom="partials/settings/facebook.html", status=True,
			fields=(
		Field("fb_app_id", "App ID"),
		Field("fb_app_secret", "App Secret", "secret"),
		Field("fb_ru_page_id", "RU Page ID"),
		Field("fb_ru_page_access_token", "RU Page Access Token", "secret"),
		Field("fb_en_page_id", "EN Page ID"),
		Field("fb_en_page_access_token", "EN Page Access Token", "secret"),
	)),
	Section("instagram", "Instagram Reels", module="instagram", badge=_instagram_badge, fields=(
		Field("ig_user_id", "IG Business Account ID", placeholder="17841400000000000", pattern=r"\d+",
			  pattern_error="The account ID is a number"),
		Field("ig_access_token", "Access Token (Page token of the page linked to IG)", "secret", placeholder="EAAB..."),
	)),
	Section("reddit", "Reddit", module="reddit", status=True, fields=(
		Field("reddit_client_id", "Client ID"),
		Field("reddit_client_secret", "Client Secret", "secret"),
		Field("reddit_username", "Username", clean=_lstrip_at),
		Field("reddit_password", "Password", "secret", placeholder="••••"),
		Field("reddit_subreddit_en", "Subreddit (EN, without r/)", placeholder="gamedev", clean=_subreddit,
			  help=f"subreddits in other languages: {_TO_DESTINATIONS}"),
		elsewhere("reddit_subreddit_ru", _DESTINATIONS, "Subreddit (RU)"),
	)),
	Section("tiktok", "TikTok", module="tiktok", custom="partials/settings/tiktok.html", status=True,
			groups=(("opts", ("tiktok_export_dir",)),), fields=(
		Field("tiktok_client_key", "Client Key", placeholder="sbaw..."),
		Field("tiktok_client_secret", "Client Secret", "secret"),
		Field("tiktok_redirect_uri", "Redirect URI (must match the TikTok app setting exactly)", "url"),
		Field("tiktok_export_dir", "Export folder", blank="default", placeholder="render",
			  help="without a token, videos for TikTok and Instagram are saved here for a manual upload"),
		elsewhere("tiktok_want_stats", "🎵 TikTok: the views & followers switch (asks for more scopes on the "
				  "next connect)", "Ask for stats", "bool"),
		state("tiktok_access_token", "Connect TikTok"),
		state("tiktok_refresh_token", "Connect TikTok"),
		state("tiktok_token_expires_at", "Connect TikTok: when the access token expires"),
		state("tiktok_scope", "what the last connect granted"),
		state("tiktok_open_id", "whose account it is"),
		state("tiktok_connected_at", "when it was connected"),
		state("oauth_state_tiktok", "the one-time state of the connect flow"),
	)),
	Section("openai", "AI & translation", status=True, actions=(
		Action("Test OpenAI", "/api/translate/test_openai", "get", include="openai_model"),
		Action("Test Claude", "/api/translate/test_claude", "get", include="anthropic_model"),
		Action("Test DeepL", "/api/translate/test_deepl", "get"),
	), fields=(
		Field("llm_provider", "The AI for translation and outreach letters", "select", choices=(
			("auto", "Auto: OpenAI if it has a key, otherwise Claude"),
			("openai", "OpenAI (ChatGPT)"),
			("anthropic", "Claude"),
		)),
		Field("openai_api_key", "API Key", "secret", placeholder="sk-...", heading="OpenAI"),
		Field("openai_model", "Model", placeholder="gpt-6-luna", blank="default"),
		Field("anthropic_api_key", "API Key", "secret", placeholder="sk-ant-...", heading="Claude"),
		Field("anthropic_model", "Model", placeholder="claude-opus-5-5", blank="default"),
		Field("deepl_api_key", "API Key", "secret", placeholder="…:fx for a free key",
			  heading="DeepL <span class=\"muted\">(machine translation, optional)</span>"),
	)),
	Section("video", "Video & music", context=_music_context, fields=(
		Field("music_dir", "Music folders", "textarea", help="one per line; subfolders are read too",
			  placeholder="E:\\Music\\Shorts\nD:\\Assets\\Music", clean=_music_dirs, display=_music_display,
			  below="partials/settings/music_folders.html"),
		Field("discord_upload_limit_mb", "Discord file limit, MB", "number", min=1, max=500, blank="default",
			  module="discord", help="10 = no boosts; 50 = boost level 2; 100 = level 3"),
	)),
	Section("updates", "Updates", custom="partials/settings/updates.html", frame=False, fields=(
		Field("update_mode", "When a new version is out", "select", choices=(
			("notify", "Tell me (a banner here, a balloon from the tray)"),
			("auto", "Install it by itself when nothing is publishing"),
			("off", "Don't check"),
		)),
		Field("update_repo", "GitHub repository", help="owner/name", placeholder="Sargonius/IndiePulseNew",
			  clean=_github_repo, blank="default", pattern=r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+",
			  pattern_error="The repository is owner/name, e.g. Sargonius/IndiePulseNew."),
		Field("update_token", "Access token", "secret", help="a private repository needs one",
			  placeholder="github_pat_…", clearable=True),
	)),
	Section("outreach", "Creator outreach", module="outreach", fields=(
		elsewhere("yt_api_key", "📣 Outreach: the YouTube Data API key for creator search", "YouTube API key",
				  "secret"),
	)),
	Section("modules", "Modules", fields=(
		elsewhere("modules_disabled", "the 🧩 Modules page", "Modules turned off"),
	)),
]

BY_ID: dict[str, Section] = {s.id: s for s in SECTIONS}
FIELDS: dict[str, Field] = {f.key: f for s in SECTIONS for f in s.fields}
OWNER: dict[str, str] = {f.key: s.id for s in SECTIONS for f in s.fields}


def section(section_id: str) -> Optional[Section]:
	return BY_ID.get(section_id)


def on(s: Section) -> bool:
	"""Its module is on (an app-wide section always is)."""
	if not s.module:
		return True
	from app.core import modules
	return modules.on(s.module)


def group_keys(s: Section, name: str) -> Optional[tuple]:
	return dict(s.groups).get(name) if name else None


def visible_fields(s: Section, keys=None) -> tuple:
	"""The form fields shown now: `keys` (a hand-written section's group of them) or all; a field of a module
	that is off is left out."""
	from app.core import modules
	out = []
	for f in s.form_fields:
		if keys is not None and f.key not in keys:
			continue
		if f.module and not modules.on(f.module):
			continue
		out.append(f)
	return tuple(out)


# ── checking what a form sent ────────────────────────────────────────────────

class Keep:
	"""parse_field's answer for a secret left empty: the saved one stays."""


KEEP = Keep()


def _default(f: Field):
	from app.services.settings import DEFAULTS
	return DEFAULTS.get(f.key)


def parse_field(f: Field, raw, clear: bool = False):
	"""One field's submitted value → (value to store | KEEP, error or "")."""
	if f.type == "bool":
		return str(raw or "").strip().lower() in ("1", "true", "on", "yes"), ""
	value = "" if raw is None else str(raw)
	if f.type != "textarea":
		value = value.strip()
	if f.type == "secret":
		if clear and f.clearable:
			return "", ""
		if not value.strip():
			return KEEP, ""
		return value.strip(), ""
	if f.clean is not None:
		value = f.clean(value)
	value = value.strip()
	if not value:
		if f.required:
			return KEEP, f"{f.label or f.key}: needed"
		if f.blank == "default" or f.type in ("number", "select"):
			return _default(f), ""
		return "", ""
	if f.type == "number":
		try:
			n = int(float(value))
		except ValueError:
			return KEEP, f"{f.label}: a number"
		if f.min is not None:
			n = max(f.min, n)
		if f.max is not None:
			n = min(f.max, n)
		return n, ""
	if f.type == "select":
		if value not in {c[0] for c in f.choices}:
			return KEEP, f"{f.label}: pick one of the list"
		return value, ""
	if f.type == "url" and not re.match(r"^https?://\S+$", value):
		return KEEP, f"{f.label}: a web address, starting with https://"
	if f.pattern and not re.fullmatch(f.pattern, value):
		return KEEP, f.pattern_error or f"{f.label}: not in the expected form"
	return value, ""


def parse_form(s: Section, form, keys=None) -> tuple[dict, dict]:
	"""What a section's form sent → (updates for save_settings, {key: error}). Only the fields shown are read:
	an unchecked checkbox is False, a secret left empty keeps the saved one. With errors, save nothing."""
	updates: dict = {}
	errors: dict = {}
	for f in visible_fields(s, keys):
		value, error = parse_field(f, form.get(f.key), clear=bool(form.get(f"{f.key}__clear")))
		if error:
			errors[f.key] = error
		elif value is not KEEP:
			updates[f.key] = value
	return updates, errors


def parse_legacy(form) -> dict:
	"""The old /api/settings/save: any form field of any section, stored only when it isn't empty (a field it
	doesn't recognise, or can't read, is skipped — as before)."""
	updates: dict = {}
	for key, raw in form.items():
		f = FIELDS.get(key)
		if f is None or not f.editable or f.type == "bool" or not str(raw or "").strip():
			continue
		value, error = parse_field(f, raw)
		if not error and value is not KEEP:
			updates[key] = value
	return updates


# ── what the templates get ───────────────────────────────────────────────────

def field_view(f: Field, cfg: dict, submitted=None, error: str = "") -> dict:
	if f.type == "secret":
		value = ""
	elif submitted is not None and f.type == "bool":
		value = f.key in submitted
	elif submitted is not None and f.key in submitted:
		value = submitted.get(f.key)
	elif f.display is not None:
		value = f.display(cfg)
	else:
		value = cfg.get(f.key)
		if value is None:
			value = _default(f)
	if f.type == "bool":
		value = str(value).lower() in ("1", "true", "on", "yes") if isinstance(value, str) else bool(value)
	elif value is None:
		value = ""
	return {"f": f, "value": value, "hint": _hint(cfg.get(f.key)) if f.type == "secret" else "", "error": error}


def form_view(s: Section, cfg: dict, keys=None, group: str = "", submitted=None, errors=None, note: str = "",
			  note_error: bool = False, actions: bool = True) -> dict:
	"""Everything partials/settings_form.html needs for one form of a section."""
	errors = errors or {}
	fields = visible_fields(s, keys)
	return {
		"section": s,
		"id": f"sf_{s.id}" + (f"_{group}" if group else ""),
		"group": group,
		"keys": ",".join(f.key for f in fields) if keys is not None else "",
		"fields": [field_view(f, cfg, submitted, errors.get(f.key, "")) for f in fields],
		"actions": s.actions if actions and not group else (),
		"note": note,
		"note_error": note_error,
		"errors": [errors[k] for k in errors],
		"ctx": s.context(cfg) if s.context else {},
	}
