"""Destinations: every place IndiePulse publishes to, each with the language its posts go out in.

Two Telegram channels (RU and JA), a Discord webhook per language, a Facebook Page per language, a subreddit per
language — and one destination for each network that is a single account (VK, X, Bluesky, YouTube, TikTok,
Instagram), whose language says which text it gets. A post published to a destination (Post.destination_id)
takes the text in that destination's language, else the first language's, else any text
(app.services.languages.text_with_lang).

Kept as a list in the settings (`destinations`), not a table: what tells two destinations of a network apart is a
credential like the rest of .settings.json (a webhook URL, a Page token), the publishers and the Settings page
already read the settings, and the per-language keys the list was built from live there too. A destination built
from such a key stays linked to it (`linked`: {field: settings key}): its value is read from that key and saved to
it. So the old Settings fields, the Facebook "fetch pages" flow, posts queued before destinations existed (no
destination_id: published by the old rules, to the same keys) and a rollback to an older IndiePulse all see one
and the same value.

One destination:
	{"id": "discord-en", "module": "discord", "lang": "en", "label": "", "enabled": true,
	 "target": {}, "linked": {"webhook": "discord_webhook_en"}, "platform": "discord", "skip_without_text": true}
- id: never changes (posts refer to it); module: the 🧩 module (app.core.modules) it belongs to — a module that
  is off hides its destinations and the worker doesn't publish to them;
- target: the values of KINDS[module].fields that aren't linked to a settings key;
- platform: the Post.platform its posts get (Facebook keeps fb_ru / fb_en: metrics, labels and size limits know
  those two);
- skip_without_text: no text in its own language → nothing goes there, instead of a text in another language
  (what the old two-webhook Discord did: a post without an EN text never reached the EN channel).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from loguru import logger

from app.services import languages

SETTINGS_KEY = "destinations"


@dataclass(frozen=True)
class TargetField:
	key: str
	label: str
	placeholder: str = ""
	secret: bool = False   # shown masked; left empty in the editor = keep the saved value


@dataclass(frozen=True)
class Kind:
	module: str
	name: str
	platform: str                       # Post.platform of its posts
	fields: tuple = ()                  # what one destination of it targets; () = the account itself
	account: tuple = ()                 # settings keys its account needs before anything can go out
	video_only: bool = False            # takes nothing but a video (the post form offers it for a video only)
	unit: str = ""                      # what one destination of it is called: "channel", "webhook"...

	@property
	def multi(self) -> bool:
		"""Several destinations possible (several channels / webhooks / Pages / subreddits)."""
		return bool(self.fields)

	def platform_for(self, lang: str) -> str:
		if self.module == "facebook":
			return "fb_ru" if lang == "ru" else "fb_en"
		return self.platform


_REDDIT_ACCOUNT = ("reddit_client_id", "reddit_client_secret", "reddit_username", "reddit_password")

# In the order the post forms list them.
KINDS: dict[str, Kind] = {k.module: k for k in (
	Kind("vk", "VK", "vk", account=("vk_access_token", "vk_group_id")),
	Kind("x", "X / Twitter", "x", account=("x_access_token",)),
	Kind("discord", "Discord", "discord",
		 (TargetField("webhook", "Webhook URL", "https://discord.com/api/webhooks/…"),), unit="webhook"),
	Kind("facebook", "Facebook", "fb_en",
		 (TargetField("page_id", "Page ID", "1234567890"),
		  TargetField("page_token", "Page access token", "EAAB…", secret=True)), unit="Page"),
	Kind("bluesky", "Bluesky", "bluesky", account=("bluesky_handle", "bluesky_app_password")),
	Kind("reddit", "Reddit", "reddit", (TargetField("subreddit", "Subreddit", "gamedev"),), account=_REDDIT_ACCOUNT,
		 unit="subreddit"),
	Kind("telegram", "Telegram", "tg", (TargetField("chat_id", "Chat ID", "-1001234567890"),),
		 account=("tg_bot_token",), unit="channel"),
	Kind("youtube", "YouTube", "yt", account=("yt_access_token", "yt_refresh_token"), video_only=True),
	# without a token these two export the file for a manual upload, so they always "work"
	Kind("tiktok", "TikTok", "tiktok", video_only=True),
	Kind("instagram", "Instagram Reels", "ig_reels", video_only=True),
)}

# What each post form offers. The video forms publish VK as a video (vk_video): the video form, the Shorts
# factory (in the order its networks were listed) and the cross-post page of a short (YouTube is where the short
# already is; TikTok has its own pipeline, /tiktok).
FORMS = {
	"post": ("vk", "x", "discord", "facebook", "bluesky", "reddit", "telegram", "youtube", "tiktok"),
	"video": ("youtube", "tiktok", "vk"),
	"shorts": ("youtube", "tiktok", "instagram", "vk", "telegram", "x", "reddit", "discord"),
	"repost": ("instagram", "x", "bluesky", "reddit", "facebook", "vk", "telegram", "discord"),
}
VIDEO_FORMS = ("video", "shorts", "repost")

# Where a post without a destination goes by the platform's old rules, as the settings keys it reads: the
# destinations linked to them are "the same place" for such a post (an earlier repost, a follower count).
LEGACY_KEYS = {
	"tg": ("tg_chat_id_ru",),
	"discord": ("discord_webhook_ru", "discord_webhook_en", "discord_webhook_url"),
	"fb_ru": ("fb_ru_page_id",),
	"fb_en": ("fb_en_page_id",),
	"reddit": ("reddit_subreddit_en",),
}


def _has(cfg: dict, key: str) -> bool:
	return bool(str(cfg.get(key) or "").strip())


def _cfg(cfg: Optional[dict]) -> dict:
	if cfg is not None:
		return cfg
	from app.services.settings import load_settings
	return load_settings()


# ── migration from the per-language keys ─────────────────────────────────────

def _dest(id: str, module: str, lang: str, linked: Optional[dict] = None, enabled: bool = True,
		  platform: Optional[str] = None, skip_without_text: bool = False) -> dict:
	return {"id": id, "module": module, "lang": lang, "label": "", "enabled": enabled, "target": {},
			"linked": dict(linked or {}), "platform": platform or KINDS[module].platform_for(lang),
			"skip_without_text": skip_without_text}


def legacy_destinations(cfg: dict) -> list[dict]:
	"""Today's setup as destinations, every one linked to the key it came from and with the language its
	platform published in. Where a network has several slots: the ones in use, or the slots the Settings page
	edits when none is."""
	out = [_dest("vk", "vk", "ru"), _dest("x", "x", "en")]
	# one post went to the RU and the EN webhook, each with its own text and only when that text was there
	hooks = [lang for lang in ("ru", "en") if _has(cfg, f"discord_webhook_{lang}")]
	for lang in hooks:
		out.append(_dest(f"discord-{lang}", "discord", lang, {"webhook": f"discord_webhook_{lang}"},
						 skip_without_text=True))
	if not hooks:  # the single webhook of the Settings page got text_en, else text_ru
		out.append(_dest("discord", "discord", "en", {"webhook": "discord_webhook_url"}))
	pages = [lang for lang in ("ru", "en") if _has(cfg, f"fb_{lang}_page_id") or _has(cfg, f"fb_{lang}_page_access_token")]
	for lang in pages or ["ru", "en"]:
		out.append(_dest(f"facebook-{lang}", "facebook", lang,
						 {"page_id": f"fb_{lang}_page_id", "page_token": f"fb_{lang}_page_access_token"}))
	out.append(_dest("bluesky", "bluesky", "en"))
	subs = [lang for lang in ("ru", "en") if _has(cfg, f"reddit_subreddit_{lang}")] or ["en"]
	for lang in subs:
		# only the EN subreddit was ever posted to: the RU one comes in turned off, so nothing new goes there
		# until it is turned on
		out.append(_dest(f"reddit-{lang}", "reddit", lang, {"subreddit": f"reddit_subreddit_{lang}"},
						 enabled=(lang == "en")))
	out.append(_dest("telegram-ru", "telegram", "ru", {"chat_id": "tg_chat_id_ru"}))
	out += [_dest("youtube", "youtube", "en"), _dest("tiktok", "tiktok", "en"), _dest("instagram", "instagram", "en")]
	return out


def migrate_settings() -> bool:
	"""Build the destinations list on the first start of this version; True when it did. Idempotent: once the
	list is stored (even empty) it is the user's and is never rebuilt. The per-language keys stay."""
	from app.services.settings import load_settings, save_settings
	cfg = load_settings()
	if isinstance(cfg.get(SETTINGS_KEY), list):
		return False
	dests = legacy_destinations(cfg)
	save_settings({SETTINGS_KEY: dests})
	if not isinstance(load_settings().get(SETTINGS_KEY), list):
		# save_settings refuses to write over an unreadable file: the next start tries again, and until then
		# stored() keeps answering with the same legacy set
		logger.error("destinations: not migrated, the settings file could not be written")
		return False
	logger.info(f"destinations: migrated from the per-language settings: {', '.join(d['id'] for d in dests)}")
	return True


# ── reading ──────────────────────────────────────────────────────────────────

def _clean(raw, langs: list[str]) -> Optional[dict]:
	if not isinstance(raw, dict):
		return None
	did = str(raw.get("id") or "").strip()
	module = str(raw.get("module") or "")
	kind = KINDS.get(module)
	if not did or kind is None:
		return None
	lang = languages.normalize_code(raw.get("lang")) or langs[0]
	fields = {f.key for f in kind.fields}
	linked = {k: str(v) for k, v in (raw.get("linked") or {}).items() if k in fields and v} \
		if isinstance(raw.get("linked"), dict) else {}
	target = {k: str(v or "") for k, v in (raw.get("target") or {}).items() if k in fields} \
		if isinstance(raw.get("target"), dict) else {}
	return {"id": did, "module": module, "lang": lang, "label": str(raw.get("label") or "").strip(),
			"enabled": raw.get("enabled", True) is not False, "target": target, "linked": linked,
			"platform": str(raw.get("platform") or "") or kind.platform_for(lang),
			"skip_without_text": bool(raw.get("skip_without_text"))}


def stored(cfg: Optional[dict] = None) -> list[dict]:
	"""Every destination as stored (linked values not filled in). Before the migration ran: the legacy set."""
	cfg = _cfg(cfg)
	raw = cfg.get(SETTINGS_KEY)
	if not isinstance(raw, list):
		raw = legacy_destinations(cfg)
	langs = languages.configured(cfg)
	out, seen = [], set()
	for item in raw:
		d = _clean(item, langs)
		if d is not None and d["id"] not in seen:
			seen.add(d["id"])
			out.append(d)
	return out


def resolve(dest: dict, cfg: Optional[dict] = None) -> dict:
	"""The destination with its target filled in: a linked field reads its settings key."""
	cfg = _cfg(cfg)
	out = dict(dest)
	target = dict(dest.get("target") or {})
	for field, key in (dest.get("linked") or {}).items():
		target[field] = str(cfg.get(key) or "").strip()
	out["target"] = target
	return out


def all_destinations(cfg: Optional[dict] = None) -> list[dict]:
	cfg = _cfg(cfg)
	return [resolve(d, cfg) for d in stored(cfg)]


def get(dest_id: Optional[str], cfg: Optional[dict] = None) -> Optional[dict]:
	"""The destination with this id, its target filled in; None when there is none (removed)."""
	if not dest_id:
		return None
	cfg = _cfg(cfg)
	for d in stored(cfg):
		if d["id"] == dest_id:
			return resolve(d, cfg)
	return None


def module_on(dest: dict) -> bool:
	from app.core import modules
	return modules.on(dest["module"])


def ready(dest: dict, cfg: Optional[dict] = None) -> bool:
	"""Its account is connected and its target filled in (resolved destination)."""
	cfg = _cfg(cfg)
	kind = KINDS.get(dest.get("module") or "")
	if kind is None:
		return False
	if kind.module == "x":
		account_ok = _has(cfg, "x_access_token") or _has(cfg, "x_refresh_token")
	else:
		account_ok = all(_has(cfg, k) for k in kind.account)
	target = dest.get("target") or {}
	return account_ok and all(str(target.get(f.key) or "").strip() for f in kind.fields)


def label(dest: dict) -> str:
	kind = KINDS.get(dest.get("module") or "")
	return dest.get("label") or (kind.name if kind else dest.get("id") or "")


def title(dest: dict) -> str:
	"""'Discord · EN', 'JP server · JA': how a destination is named on pages."""
	return f"{label(dest)} · {(dest.get('lang') or '').upper()}"


def post_platform(dest: dict, form: str = "post") -> str:
	"""The Post.platform of a post to this destination (VK from a video form = a VK video)."""
	if dest["module"] == "vk" and form in VIDEO_FORMS:
		return "vk_video"
	return dest.get("platform") or KINDS[dest["module"]].platform_for(dest["lang"])


def takes_legacy(dest: dict, platform: str) -> bool:
	"""A post to `platform` without a destination (the old rules) went where this destination goes: the
	network's one account, or the old settings key this destination is linked to."""
	kind = KINDS.get(dest.get("module") or "")
	if kind is None or platform not in (post_platform(dest, "post"), post_platform(dest, "video")):
		return False
	if not kind.multi:
		return True
	return bool(set(dest.get("linked", {}).values()) & set(LEGACY_KEYS.get(platform, ())))


def lacks_own_text(dest: dict, brief) -> bool:
	"""A destination that takes only its own language, and the brief has no text in it."""
	return bool(dest.get("skip_without_text")) and \
		not (languages.brief_texts(brief).get(dest.get("lang")) or "").strip()


def unavailable(dest_id: str, cfg: Optional[dict] = None) -> Optional[str]:
	"""Why nothing may go to this destination now: 'removed' / 'off'; None = it may."""
	dest = get(dest_id, cfg)
	if dest is None:
		return "removed"
	if not dest["enabled"]:
		return "off"
	return None


def for_form(form: str = "post", cfg: Optional[dict] = None) -> list[dict]:
	"""The post form's destination checkboxes, grouped by network: only modules that are on and destinations
	that are turned on. [{module, name, video_only, destinations: [{id, label, lang, platform, ready, title}]}]"""
	cfg = _cfg(cfg)
	groups: dict[str, dict] = {}
	order = FORMS.get(form, FORMS["post"])
	dests = [d for d in all_destinations(cfg) if d["module"] in order and d["enabled"] and module_on(d)]
	for d in sorted(dests, key=lambda d: order.index(d["module"])):
		kind = KINDS[d["module"]]
		group = groups.setdefault(d["module"], {"module": d["module"], "name": kind.name,
												"video_only": kind.video_only and form not in VIDEO_FORMS,
												"destinations": []})
		group["destinations"].append({"id": d["id"], "label": label(d), "lang": d["lang"], "title": title(d),
									  "platform": post_platform(d, form), "ready": ready(d, cfg),
									  "own_only": d["skip_without_text"]})
	return list(groups.values())


def chosen(ids, form: str = "post", cfg: Optional[dict] = None) -> tuple[list[dict], list[str]]:
	"""The destinations a form submitted, resolved → (usable, skipped ids: unknown, turned off, module off)."""
	cfg = _cfg(cfg)
	by_id = {d["id"]: d for d in all_destinations(cfg)}
	usable, skipped, seen = [], [], set()
	for did in ids or []:
		did = str(did or "").strip()
		if not did or did in seen:
			continue
		seen.add(did)
		d = by_id.get(did)
		if d is None or not d["enabled"] or not module_on(d) or d["module"] not in FORMS.get(form, ()):
			skipped.append(did)
			continue
		usable.append(d)
	return usable, skipped


# ── the Settings editor ──────────────────────────────────────────────────────

def _mask(value: str) -> str:
	return ("•••" + value[-4:]) if value else ""


def editor_context(cfg: Optional[dict] = None) -> dict:
	"""What Settings → Languages & destinations shows: the languages and, per network, its destinations
	(secrets masked) — JSON-ready. Networks of modules that are off are marked so."""
	from app.core import modules
	cfg = _cfg(cfg)
	langs = languages.configured(cfg)
	dests = all_destinations(cfg)
	groups = []
	for module, kind in KINDS.items():
		rows = []
		for d in dests:
			if d["module"] != module:
				continue
			target = {}
			for f in kind.fields:
				val = d["target"].get(f.key, "")
				target[f.key] = "" if f.secret else val
				if f.secret:
					target[f.key + "_hint"] = _mask(val)
			rows.append({"id": d["id"], "lang": d["lang"], "label": d["label"], "enabled": d["enabled"],
						 "skip_without_text": d["skip_without_text"], "target": target,
						 "linked": sorted(d["linked"].values()), "ready": ready(d, cfg)})
		groups.append({"module": module, "name": kind.name, "multi": kind.multi, "unit": kind.unit,
					   "on": modules.on(module),
					   "fields": [{"key": f.key, "label": f.label, "placeholder": f.placeholder, "secret": f.secret}
								  for f in kind.fields],
					   "destinations": rows})
	return {"languages": [{"code": c, "name": languages.name(c)} for c in langs],
			"known_languages": [{"code": c, "name": n} for c, n in languages.NAMES.items()],
			"groups": groups}


def _new_id(module: str, lang: str, taken: set) -> str:
	base = f"{module}-{lang}"
	did, n = base, 2
	while did in taken:
		did, n = f"{base}-{n}", n + 1
	return did


def save(langs_in, items, cfg: Optional[dict] = None) -> dict:
	"""Store what the editor sent: {"languages": [...], "destinations": [{id?, module, lang, label, enabled,
	skip_without_text, target}]}. → {"ok": True, "languages", "destinations"} or {"ok": False, "errors"}.

	A linked field is written to its settings key; a field not sent, or a secret left empty, keeps what was
	saved; a destination
	without an id (or with one that doesn't exist) is new and gets an id. Nothing is saved when anything is wrong.
	"""
	from app.services.settings import save_settings
	cfg = _cfg(cfg)
	errors: list[str] = []
	langs: list[str] = []
	for code in langs_in or []:
		c = languages.normalize_code(code)
		if c is None:
			errors.append(f"“{code}” is not a language code (two or three letters, like ja or pt-br)")
		elif c not in langs:
			langs.append(c)
	if not langs:
		errors.append("Keep at least one language: the one you write in")
	prev = {d["id"]: d for d in stored(cfg)}
	taken = set(prev)
	out: list[dict] = []
	key_updates: dict = {}
	singles: dict = {}
	for item in items or []:
		if not isinstance(item, dict):
			continue
		module = str(item.get("module") or "")
		kind = KINDS.get(module)
		if kind is None:
			errors.append(f"Unknown network: {module}")
			continue
		lang = languages.normalize_code(item.get("lang"))
		if lang is None or lang not in langs:
			errors.append(f"{kind.name}: its language {(item.get('lang') or '?').upper()} isn't in the languages list")
			continue
		if not kind.multi:
			singles[module] = singles.get(module, 0) + 1
		old = prev.get(str(item.get("id") or ""))
		if old is not None and old["module"] != module:
			old = None
		did = old["id"] if old else _new_id(module, lang, taken)
		taken.add(did)
		linked = dict(old["linked"]) if old else {}
		old_target = resolve(old, cfg)["target"] if old else {}
		raw_target = item.get("target") if isinstance(item.get("target"), dict) else {}
		target = {}
		for f in kind.fields:
			val = str(raw_target.get(f.key) or "").strip()
			if f.key not in raw_target or (f.secret and not val):
				val = old_target.get(f.key, "")  # not sent / a secret left empty: keep it
			if f.key in linked:
				if val != old_target.get(f.key, ""):
					key_updates[linked[f.key]] = val
			else:
				target[f.key] = val
		out.append({"id": did, "module": module, "lang": lang, "label": str(item.get("label") or "").strip()[:60],
					"enabled": item.get("enabled", True) is not False, "target": target, "linked": linked,
					"platform": old["platform"] if old else kind.platform_for(lang),
					"skip_without_text": bool(item.get("skip_without_text"))})
	for module, n in singles.items():
		if n > 1:
			errors.append(f"{KINDS[module].name} is one account: it can have one destination (pick its language)")
	if errors:
		return {"ok": False, "errors": errors}
	save_settings({"languages": langs, SETTINGS_KEY: out, **key_updates})
	logger.info(f"destinations saved: languages={langs} destinations={[d['id'] for d in out]} "
				f"linked keys updated={sorted(key_updates)}")
	return {"ok": True, "languages": langs, "destinations": [d["id"] for d in out]}
