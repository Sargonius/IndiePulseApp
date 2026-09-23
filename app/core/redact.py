"""Strip credentials out of text before it reaches logs, the DB or the UI.

Two layers: every secret-looking value currently in .settings.json, plus patterns
for secrets that live inside URLs (Telegram bot token, Discord webhook token) —
httpx/requests put the full URL into their exception messages.
"""
from __future__ import annotations

import json
import os
import re

_SECRET_MARKS = ("token", "secret", "password", "key", "webhook")
_PATTERNS = [
    (re.compile(r"bot\d{5,}:[A-Za-z0-9_-]{20,}"), "bot•••"),
    (re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_.-]+"), r"\1•••"),
    (re.compile(r"((?:access_token|client_secret|refresh_token)=)[^&\s'\"]+"), r"\1•••"),
]
_cache: dict = {"key": None, "values": []}


def _settings_secrets() -> list[str]:
    # Read the file directly (not via load_settings) — this runs inside the log
    # patcher, and load_settings itself logs.
    from app.services import settings as _settings
    path = os.path.abspath(_settings.SETTINGS_PATH)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if (path, mtime) != _cache["key"]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            vals = {str(v) for k, v in data.items()
                    if isinstance(v, str) and len(v) >= 8 and any(m in k for m in _SECRET_MARKS)}
            _cache["values"] = sorted(vals, key=len, reverse=True)
            _cache["key"] = (path, mtime)
        except Exception:
            pass
    return _cache["values"]


def redact(text, extra: list[str] | None = None):
    if text is None:
        return None
    text = str(text)
    for secret in (extra or []) + _settings_secrets():
        if secret and secret in text:
            text = text.replace(secret, "•••")
    for rx, repl in _PATTERNS:
        text = rx.sub(repl, text)
    return text
