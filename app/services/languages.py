"""The languages a publication is written in, and its text in each of them.

Settings → Languages & destinations keeps an ordered list of language codes (`languages`): the first is the
language you write in, the others are translations of it. A Brief keeps one text per language in `texts_json`
({lang: text}). Its old `text_ru` / `text_en` columns stay as mirrors of the "ru" and "en" entries, so every row
written before and every piece of code that still reads or writes them keeps working:

- read with brief_texts() / brief_text(), write with set_brief_texts(): they keep both sides in step;
- code that writes text_ru / text_en directly is covered by an ORM hook (sync_mirrors, models.py): on save the
  columns win over texts_json for "ru" and "en", because they were the last thing written.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, Optional

DEFAULT_LANGUAGES = ("ru", "en")
# languages with a column of their own on Brief (text_ru, text_en)
MIRRORED = ("ru", "en")
NAMES = {
	"en": "English", "ru": "Russian", "ja": "Japanese", "de": "German", "fr": "French", "es": "Spanish",
	"pt": "Portuguese", "pt-br": "Portuguese (Brazil)", "it": "Italian", "pl": "Polish", "uk": "Ukrainian",
	"tr": "Turkish", "zh": "Chinese", "zh-hans": "Chinese (Simplified)", "zh-hant": "Chinese (Traditional)",
	"ko": "Korean", "nl": "Dutch", "sv": "Swedish", "cs": "Czech", "fi": "Finnish", "da": "Danish",
	"no": "Norwegian", "hu": "Hungarian", "ro": "Romanian", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese",
	"ar": "Arabic", "he": "Hebrew", "hi": "Hindi", "el": "Greek", "bg": "Bulgarian", "kk": "Kazakh",
}
_CODE = re.compile(r"^[a-z]{2,3}(-[a-z0-9]{2,8})?$")


def normalize_code(code) -> Optional[str]:
	"""'JA' → 'ja', 'pt_BR' → 'pt-br'; None for anything that isn't a language code."""
	c = str(code or "").strip().lower().replace("_", "-")
	return c if _CODE.match(c) else None


def name(code: str) -> str:
	return NAMES.get(code) or (code or "").upper()


def configured(cfg: Optional[dict] = None) -> list[str]:
	"""The languages from Settings in their order (first = the one you write in); never empty."""
	if cfg is None:
		from app.services.settings import load_settings
		cfg = load_settings()
	raw = cfg.get("languages")
	out: list[str] = []
	for code in raw if isinstance(raw, (list, tuple)) else ():
		c = normalize_code(code)
		if c and c not in out:
			out.append(c)
	return out or list(DEFAULT_LANGUAGES)


def source(cfg: Optional[dict] = None) -> str:
	return configured(cfg)[0]


def _parse(raw) -> dict:
	try:
		data = json.loads(raw) if raw else {}
	except (TypeError, ValueError):
		return {}
	if not isinstance(data, dict):
		return {}
	out = {}
	for k, v in data.items():
		c = normalize_code(k)
		if c and v is not None:
			out[c] = str(v)
	return out


def brief_texts(brief) -> dict:
	"""{lang: text} of a Brief (or its publish copy), "ru" and "en" read from their columns."""
	if brief is None:
		return {}
	texts = _parse(getattr(brief, "texts_json", None))
	for lang in MIRRORED:
		col = getattr(brief, f"text_{lang}", None)
		if col is not None:
			texts[lang] = col
	return texts


def pick(texts: dict, order: Iterable[str]) -> tuple[str, Optional[str]]:
	"""(text, its language): the first language in `order` with a text, else any text at all."""
	seen = []
	for lang in list(order) + [k for k in texts if k not in order]:
		if lang in seen:
			continue
		seen.append(lang)
		text = texts.get(lang) or ""
		if text.strip():
			return text, lang
	return "", None


def text_with_lang(brief, lang: str, cfg: Optional[dict] = None) -> tuple[str, Optional[str]]:
	"""The brief's text for `lang`; without one, the first language's, then any language's — and which it was."""
	return pick(brief_texts(brief), [lang] + configured(cfg))


def brief_text(brief, lang: str, cfg: Optional[dict] = None) -> str:
	return text_with_lang(brief, lang, cfg)[0]


def set_brief_texts(brief, texts: dict) -> None:
	"""Set some or all of the brief's texts ({lang: text}); the other languages keep theirs."""
	merged = brief_texts(brief)
	for k, v in (texts or {}).items():
		c = normalize_code(k)
		if c:
			merged[c] = str(v or "")
	for lang in MIRRORED:
		if lang in merged:
			setattr(brief, f"text_{lang}", merged[lang])
	brief.texts_json = json.dumps(merged, ensure_ascii=False) if merged else None


def sync_mirrors(brief) -> None:
	"""Before a Brief is saved: texts_json and text_ru / text_en say the same (the columns win, see above)."""
	texts = _parse(getattr(brief, "texts_json", None))
	for lang in MIRRORED:
		col = getattr(brief, f"text_{lang}", None)
		if col is not None:
			texts[lang] = col
		elif lang in texts:
			setattr(brief, f"text_{lang}", texts[lang])
	new = json.dumps(texts, ensure_ascii=False) if texts else None
	if new != getattr(brief, "texts_json", None):
		brief.texts_json = new


def missing(texts: dict, langs: Iterable[str]) -> list[str]:
	return [lang for lang in langs if not (texts.get(lang) or "").strip()]


_SYSTEM = ("You are a professional localization translator for an indie game developer's social media posts. "
		   "Translate {src} to natural, idiomatic {dst}. Preserve Steam markup tags (e.g. [h1], [b], [url], [list]), "
		   "URLs, @handles, hashtags of game names, emoji, line breaks and the literal placeholder {{steam}} exactly "
		   "as they are. Do not add commentary; return only the translated text.")


def translate(text: str, target: str, src_lang: str) -> tuple[str, str]:
	"""(translation, provider) of one text: the AI from Settings, then machine translation (DeepL or the free
	translators); ("", "") when nothing worked."""
	from loguru import logger
	from app.services import llm, machine_translate
	text = (text or "").strip()
	if not text or target == src_lang:
		return text, ""
	out, provider = "", ""
	try:
		out, provider = llm.chat([
			{"role": "system", "content": _SYSTEM.format(src=name(src_lang), dst=name(target))},
			{"role": "user", "content": f"Translate to {name(target)}, preserving markup:\n\n" + text},
		], temperature=0.2, timeout=60)
	except llm.LLMError as exc:
		logger.warning(f"translate {src_lang}→{target} ai: {exc}")
	if not machine_translate.looks_translated(text, out, target):
		out, provider = machine_translate.translate(text, target, src_lang)
	return (out, provider) if out else ("", "")


def translate_missing(texts: dict, langs: Optional[list] = None, cfg: Optional[dict] = None) -> dict:
	"""Fill the empty languages from the first one with a text (the source language first).

	→ {"texts": {lang: translation}, "providers": {lang: provider}, "failed": [lang...], "source": lang}; only the
	languages that were empty are in "texts".
	"""
	langs = list(langs or configured(cfg))
	src_text, src_lang = pick(texts, langs)
	result = {"texts": {}, "providers": {}, "failed": [], "source": src_lang}
	if not src_lang:
		return result
	for lang in missing(texts, langs):
		out, provider = translate(src_text, lang, src_lang)
		if out:
			result["texts"][lang] = out
			result["providers"][lang] = provider
		else:
			result["failed"].append(lang)
	return result
