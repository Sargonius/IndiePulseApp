"""Claude (Anthropic Messages API, official SDK) for translation and outreach letters, with the user's own key.

Takes the same OpenAI-style messages as openai_client (a leading "system" message becomes `system`), so app.services.llm
can send one prompt to whichever provider the user picked. No `temperature`: the API rejects sampling parameters.
On the models that have it, a refused request is re-run by the API itself on a fallback model ("default" routing).
"""
from __future__ import annotations

from typing import Optional

from app.services.settings import load_settings

DEFAULT_MODEL = "claude-opus-5-5"
_FALLBACK_MODELS = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeError(Exception):
	pass


def settings() -> tuple[str, str]:
	cfg = load_settings()
	return (cfg.get("anthropic_api_key") or "").strip(), (cfg.get("anthropic_model") or DEFAULT_MODEL).strip()


def chat(messages: list[dict], *, max_tokens: int = 16000, model: Optional[str] = None, timeout: float = 60.0,
		 api_key: Optional[str] = None) -> tuple[str, str]:
	"""→ (answer text, model that answered). Raises ClaudeError with a short reason."""
	import anthropic

	key, cfg_model = settings()
	key = (api_key or key).strip()
	model = (model or cfg_model).strip()
	if not key:
		raise ClaudeError("no API key")
	system = "\n\n".join(str(m["content"]) for m in messages if m["role"] == "system")
	turns = [{"role": m["role"], "content": m["content"]} for m in messages if m["role"] != "system"]
	request: dict = {"model": model, "max_tokens": max_tokens, "messages": turns}
	if system:
		request["system"] = system
	client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)
	try:
		if model in _FALLBACK_MODELS:
			resp = client.beta.messages.create(betas=[_FALLBACK_BETA], fallbacks="default", **request)
		else:
			resp = client.messages.create(**request)
	except anthropic.AuthenticationError:
		raise ClaudeError("key rejected (401) — create a new one at platform.claude.com") from None
	except anthropic.PermissionDeniedError as exc:
		raise ClaudeError(f"no access (403): {exc.message}") from None
	except anthropic.NotFoundError:
		raise ClaudeError(f"model {model} not found (404)") from None
	except anthropic.RateLimitError:
		raise ClaudeError("rate limited (429) — try again in a minute") from None
	except anthropic.BadRequestError as exc:  # also an empty credit balance
		raise ClaudeError(f"request rejected (400): {exc.message}") from None
	except anthropic.APIStatusError as exc:
		raise ClaudeError(f"HTTP {exc.status_code}: {exc.message}") from None
	except anthropic.APIConnectionError:
		raise ClaudeError("network error") from None
	if resp.stop_reason == "refusal":
		raise ClaudeError("Claude declined the request")
	text = "".join(block.text for block in resp.content if block.type == "text").strip()
	return text, resp.model
