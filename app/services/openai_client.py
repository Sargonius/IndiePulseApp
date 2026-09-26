"""OpenAI Chat Completions for translation, tolerant of model generations.

Older models (gpt-4o-mini) reject `reasoning_effort`; reasoning models (GPT-5/6) may
reject a non-default `temperature`. We send what we'd like and, on a 400 that names one
of those parameters, retry without it (and remember that for the model) — so changing
the model in Settings never silently drops translation to the free fallback.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

from app.services.settings import load_settings

URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-6-luna"
# Translation needs no chain-of-thought: fastest and cheapest on reasoning models.
REASONING_EFFORT = "none"
_OPTIONAL = ("temperature", "reasoning_effort")
_unsupported: dict[str, set[str]] = {}


def settings() -> tuple[str, str]:
	cfg = load_settings()
	return (cfg.get("openai_api_key") or "").strip(), (cfg.get("openai_model") or DEFAULT_MODEL).strip()


def _rejected_param(resp: httpx.Response, payload: dict) -> Optional[str]:
	if resp.status_code != 400:
		return None
	try:
		err = resp.json().get("error") or {}
	except Exception:
		return None
	param = err.get("param")
	if param in _OPTIONAL and param in payload:
		return param
	# e.g. gpt-4o-mini: "Unrecognized request argument supplied: reasoning_effort" (param null)
	msg = str(err.get("message") or "")
	return next((p for p in _OPTIONAL if p in payload and re.search(rf"\b{p}\b", msg)), None)


def chat_completion(messages: list[dict], *, temperature: Optional[float] = None,
					max_completion_tokens: Optional[int] = None, model: Optional[str] = None,
					timeout: float = 20.0, api_key: Optional[str] = None) -> tuple[httpx.Response, str]:
	"""POST /chat/completions; returns (response, model used)."""
	key, cfg_model = settings()
	key = api_key or key
	model = (model or cfg_model).strip()
	payload: dict = {"model": model, "messages": messages, "reasoning_effort": REASONING_EFFORT}
	if temperature is not None:
		payload["temperature"] = temperature
	if max_completion_tokens is not None:
		payload["max_completion_tokens"] = max_completion_tokens
	for p in _unsupported.get(model, ()):
		payload.pop(p, None)
	headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
	with httpx.Client(timeout=timeout) as client:
		for _ in range(len(_OPTIONAL) + 1):
			resp = client.post(URL, headers=headers, json=payload)
			bad = _rejected_param(resp, payload)
			if not bad:
				break
			_unsupported.setdefault(model, set()).add(bad)
			payload.pop(bad)
	return resp, model


def content_of(resp: httpx.Response) -> str:
	try:
		data = resp.json()
	except Exception:
		return ""
	return (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
