"""The language model behind translation and outreach letters: OpenAI or Claude, with the user's own key
(⚙️ Settings → AI & translation). One prompt, either provider; callers fall back to machine_translate (or a template)
when there is no key or the call fails.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from app.services import anthropic_client, openai_client
from app.services.settings import load_settings

PROVIDERS = {"openai": "OpenAI", "anthropic": "Claude"}


class LLMError(Exception):
	pass


def provider(cfg: Optional[dict] = None) -> Optional[str]:
	"""'openai' or 'anthropic' — the one picked in Settings, or with "auto" OpenAI if it has a key, else Claude;
	None when the picked one (or both) has no key."""
	cfg = cfg if cfg is not None else load_settings()
	has = {p: bool(str(cfg.get(f"{p}_api_key") or "").strip()) for p in PROVIDERS}
	choice = str(cfg.get("llm_provider") or "auto")
	if choice in PROVIDERS:
		return choice if has[choice] else None
	return next((p for p in ("openai", "anthropic") if has[p]), None)


def chat(messages: list[dict], *, max_tokens: Optional[int] = None, temperature: Optional[float] = None,
		 timeout: float = 60.0, cfg: Optional[dict] = None) -> tuple[str, str]:
	"""→ (answer, "openai:<model>" / "claude:<model>"). `temperature` and `max_tokens` reach OpenAI only: Claude
	takes no temperature, and its thinking counts against max_tokens, so it keeps its own roomy cap.
	Raises LLMError: no key, an HTTP error, an empty answer."""
	which = provider(cfg)
	if which is None:
		raise LLMError("no AI key in Settings")
	if which == "openai":
		resp, model = openai_client.chat_completion(messages, temperature=temperature,
													max_completion_tokens=max_tokens, timeout=timeout)
		label = f"openai:{model}"
		if resp.status_code >= 400:
			raise LLMError(f"{label}: HTTP {resp.status_code} {resp.text[:200]}")
		text = openai_client.content_of(resp)
	else:
		try:
			text, model = anthropic_client.chat(messages, timeout=timeout)
		except anthropic_client.ClaudeError as exc:
			raise LLMError(f"claude: {exc}") from None
		label = f"claude:{model}"
	if not text:
		raise LLMError(f"{label}: empty answer")
	return text, label


def json_object(content: str) -> Optional[dict]:
	"""The JSON object in an answer asked to be "JSON only" — models still wrap it in ``` fences or a sentence."""
	raw = (content or "").strip()
	raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
	for candidate in (raw, (re.search(r"\{.*\}", raw, re.S) or [None])[0]):
		if not candidate:
			continue
		try:
			data = json.loads(candidate)
		except (TypeError, ValueError):
			continue
		if isinstance(data, dict):
			return data
	return None
