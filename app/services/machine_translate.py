"""Translation without a language model: DeepL with the user's key (⚙️ Settings → AI & translation), otherwise the
free public translators of deep-translator (LibreTranslate mirrors, Google, MyMemory) — what every translate button
falls back to when the AI isn't set up or fails.
"""
from __future__ import annotations

from typing import Optional

import httpx
from loguru import logger

from app.services.settings import load_settings

_LIBRE_HOSTS = ("https://libretranslate.de", "https://translate.astian.org")
_DEEPL_TARGET = {"en": "EN-US", "pt": "PT-BR", "zh": "ZH-HANS"}  # DeepL wants a variant for these


def _cyrillic(s: str) -> bool:
	return any("а" <= ch.lower() <= "я" or ch in "ёЁ" for ch in s)


def looks_translated(src: str, out: str, target: str) -> bool:
	"""Not empty, not the source back, and not still Russian when the target isn't."""
	out = (out or "").strip()
	return bool(out) and out != src.strip() and (target == "ru" or not _cyrillic(out))


def deepl(text: str, target: str, source: Optional[str] = None, key: Optional[str] = None,
		  timeout: float = 30.0) -> str:
	"""DeepL API; free keys end with ':fx' and go to the free endpoint. Raises on an HTTP error."""
	key = (key if key is not None else load_settings().get("deepl_api_key") or "").strip()
	if not key:
		raise ValueError("no DeepL key")
	host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
	data = {"text": text, "target_lang": _DEEPL_TARGET.get(target, target.upper())}
	if source:
		data["source_lang"] = source.upper()
	resp = httpx.post(f"{host}/v2/translate", data=data, headers={"Authorization": f"DeepL-Auth-Key {key}"},
					  timeout=timeout)
	resp.raise_for_status()
	return ((resp.json().get("translations") or [{}])[0].get("text") or "").strip()


def translate(text: str, target: str, source: Optional[str] = None) -> tuple[str, str]:
	"""→ (translation, provider) or ("", "") when nothing worked. `source` None = detect (DeepL, Google only)."""
	text = (text or "").strip()
	if not text:
		return "", ""
	tries = []
	if str(load_settings().get("deepl_api_key") or "").strip():
		tries.append(("deepl", lambda: deepl(text, target, source)))
	from deep_translator import GoogleTranslator, LibreTranslator, MyMemoryTranslator
	if source:
		for host in _LIBRE_HOSTS:
			tries.append((f"libre:{host}",
						  lambda h=host: LibreTranslator(source=source, target=target, host=h).translate(text)))
	tries.append(("google", lambda: GoogleTranslator(source=source or "auto", target=target).translate(text)))
	if source:
		tries.append(("mymemory", lambda: MyMemoryTranslator(source=source, target=target).translate(text)))
	for name, run in tries:
		try:
			out = (run() or "").strip()
		except Exception as exc:
			logger.warning(f"machine_translate {name} failed: {exc}")
			continue
		if looks_translated(text, out, target):
			return out, name
	return "", ""
