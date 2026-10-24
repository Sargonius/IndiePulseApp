"""What the ⚙️ Settings page and the setup wizard show: their sections, made from app.core.settings_schema, and
the values the hand-written sections (the OAuth connect flows) still read by name."""
from __future__ import annotations

from typing import Optional

from app.core import settings_schema as schema


def section_view(s: schema.Section, cfg: dict) -> dict:
	"""One section for partials/settings_section.html: the form made from its fields, or None for a hand-written
	one (its template draws the body)."""
	return {
		"section": s,
		"form": None if s.custom else schema.form_view(s, cfg),
		"badge": s.badge(cfg) if s.badge else None,
	}


def sections_view(cfg: dict, ids: Optional[list] = None, only_on: bool = False) -> list:
	"""The sections with something to edit, in page order — or those of `ids`, in that order. `only_on` leaves
	out the ones of modules that are off (the Settings page renders them and base.html hides them, as before)."""
	chosen = [schema.BY_ID[i] for i in ids if i in schema.BY_ID] if ids is not None else schema.SECTIONS
	return [section_view(s, cfg) for s in chosen if s.shown and (not only_on or schema.on(s))]


def group_forms(cfg: dict) -> dict:
	"""The forms made from fields inside the hand-written sections, by "<section>_<group>"."""
	return {f"{s.id}_{name}": schema.form_view(s, cfg, keys=keys, group=name)
			for s in schema.SECTIONS for name, keys in s.groups}


def _hint(cfg: dict, key: str) -> str:
	value = str(cfg.get(key) or "")
	return ("•••" + value[-4:]) if value else ""


def custom_context(cfg: dict) -> dict:
	"""What the hand-written sections read by name."""
	from app.services import destinations
	return {
		"dest_editor": destinations.editor_context(cfg),
		"forms": group_forms(cfg),
		"vk_client_id": cfg.get("vk_client_id", ""),
		"vk_client_secret": cfg.get("vk_client_secret", ""),
		"vk_group_id": cfg.get("vk_group_id", ""),
		"vk_token_hint": _hint(cfg, "vk_access_token"),
		"vk_user_token_hint": _hint(cfg, "vk_user_access_token"),
		"x_client_id": cfg.get("x_client_id", ""),
		"x_client_secret": cfg.get("x_client_secret", ""),
		"x_redirect_uri": cfg.get("x_redirect_uri", ""),
		"x_api_key": cfg.get("x_api_key", ""),
		"x_api_secret": cfg.get("x_api_secret", ""),
		"x_oauth_token": cfg.get("x_oauth_token", ""),
		"x_oauth_token_secret": cfg.get("x_oauth_token_secret", ""),
		"yt_client_id": cfg.get("yt_client_id", ""),
		"yt_client_secret": cfg.get("yt_client_secret", ""),
		"yt_access_token": cfg.get("yt_access_token", ""),
		"yt_refresh_token": cfg.get("yt_refresh_token", ""),
		"yt_metrics_days": cfg.get("yt_metrics_days", 90),
		"fb_app_id": cfg.get("fb_app_id", ""),
		"fb_app_secret": cfg.get("fb_app_secret", ""),
		"fb_ru_page_id": cfg.get("fb_ru_page_id", ""),
		"fb_en_page_id": cfg.get("fb_en_page_id", ""),
		"fb_ru_page_access_token": cfg.get("fb_ru_page_access_token", ""),
		"fb_en_page_access_token": cfg.get("fb_en_page_access_token", ""),
		"tiktok_client_key": cfg.get("tiktok_client_key", ""),
		"tiktok_secret_hint": _hint(cfg, "tiktok_client_secret"),
		"tiktok_redirect_uri": cfg.get("tiktok_redirect_uri"),
		"tiktok_token_hint": _hint(cfg, "tiktok_access_token"),
	}
