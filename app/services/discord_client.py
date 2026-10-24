from __future__ import annotations

import json
from typing import Optional
import os
import mimetypes

import httpx
import requests
from loguru import logger
from app.services.settings import load_settings
import re


def _split_by_sentences(text: str, max_length: int) -> list[str]:
    """Split text by sentences, keeping each chunk under max_length."""
    if not text:
        return [""]
    if len(text) <= max_length:
        return [text]
    sentences = re.findall(r'[^.!?]+[.!?]+|[^.!?]+$', text)
    chunks: list[str] = []
    current_chunk = ""
    for sentence in sentences:
        sentence = sentence.strip()
        if not sentence:
            continue
        if len(sentence) > max_length:
            words = sentence.split()
            for word in words:
                if not current_chunk:
                    current_chunk = word
                elif len(current_chunk) + 1 + len(word) <= max_length:
                    current_chunk += " " + word
                else:
                    chunks.append(current_chunk)
                    current_chunk = word
            continue
        if not current_chunk:
            current_chunk = sentence
        elif len(current_chunk) + 1 + len(sentence) <= max_length:
            current_chunk += " " + sentence
        else:
            chunks.append(current_chunk)
            current_chunk = sentence
    if current_chunk:
        chunks.append(current_chunk)
    return chunks or [text]


class DiscordSendError(RuntimeError):
    """Webhook send failed. `delivered` tells the caller whether retrying is safe:
    False = nothing reached Discord; True = some chunk was posted;
    None = unknown (response lost after the request was sent)."""

    def __init__(self, msg: str, delivered: Optional[bool]) -> None:
        super().__init__(msg)
        self.delivered = delivered


class DiscordClient:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url.strip()
        # Populated after a successful send so callers can read reactions later.
        self.last_message_id: Optional[str] = None
        self.last_channel_id: Optional[str] = None

    @staticmethod
    def get_message_reactions(bot_token: str, channel_id: str, message_id: str) -> dict:
        """Read reaction counts for one message via the Bot REST API.

        Webhooks are write-only, so reactions can only be read with a bot token whose
        bot is a member of the server and has View Channel + Read Message History on
        the channel. No gateway/websocket connection is needed — a single GET is enough.

        Returns {"like": <total reactions>, "reactions": {emoji_name: count, ...}}.
        Raises RuntimeError on an API error so the caller can record it.
        """
        token = (bot_token or "").strip()
        ch = str(channel_id or "").strip()
        msg = str(message_id or "").strip()
        if not (token and ch and msg):
            return {}
        url = f"https://discord.com/api/v10/channels/{ch}/messages/{msg}"
        headers = {"Authorization": f"Bot {token}", "User-Agent": "IndiePulse/1.0"}
        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=headers)
            if r.status_code >= 400:
                raise RuntimeError(f"discord_fetch_error status={r.status_code} body={r.text[:200]}")
            data = r.json()
        reactions = data.get("reactions") or []
        breakdown: dict[str, int] = {}
        total = 0
        for rc in reactions:
            emoji = rc.get("emoji") or {}
            name = emoji.get("name") or "?"
            count = int(rc.get("count") or 0)
            breakdown[name] = breakdown.get(name, 0) + count
            total += count
        return {"like": total, "reactions": breakdown}

    @staticmethod
    def _check_attachments(sources: list[str]) -> None:
        """A post with media must never go out as text only: refuse up front when a
        file is missing or over the server's upload limit (nothing is sent yet)."""
        try:
            limit_mb = float(load_settings().get("discord_upload_limit_mb") or 10)
        except Exception:
            limit_mb = 10.0
        for p in sources:
            name = os.path.basename(p)
            if not os.path.exists(p):
                raise DiscordSendError(f"discord: attachment not found: {name}", delivered=False)
            size_mb = os.path.getsize(p) / (1024 * 1024)
            if size_mb > limit_mb:
                raise DiscordSendError(
                    f"discord: {name} is {size_mb:.1f} MB, over the {limit_mb:g} MB upload limit",
                    delivered=False)

    def send_message(self, content: str, file_path: Optional[str] = None, file_paths: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
        # Use synchronous requests for reliability with multipart + embed
        if not self.webhook_url:
            raise ValueError("Discord webhook URL is empty")
        url = self.webhook_url + ("&wait=true" if "?" in self.webhook_url else "?wait=true")
        self.last_message_id = None
        self.last_channel_id = None
        sources = [p for p in (file_paths or ([file_path] if file_path else [])) if p]
        self._check_attachments(sources)
        try:
            chunks = _split_by_sentences(content or "", 2000)
            message_id = None
            message_url = None
            for i, chunk in enumerate(chunks):
                if sources and i == 0:
                    payload = {"content": chunk[:2000]}
                    files_multi = []
                    file_handles = []
                    try:
                        for p in sources:
                            filename = os.path.basename(p)
                            ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                            fh = open(p, 'rb')
                            file_handles.append(fh)
                            files_multi.append(('file', (filename, fh, ctype)))
                        first_name = files_multi[0][1][0]
                        first_ctype = mimetypes.guess_type(first_name)[0] or 'application/octet-stream'
                        if first_ctype.startswith('image/'):
                            payload["embeds"] = [{"image": {"url": f"attachment://{first_name}"}}]
                        # a 10 MB video needs longer than 60 s on a slow uplink
                        resp = requests.post(url, data={"payload_json": json.dumps(payload)}, files=files_multi,
                                             timeout=(15, 180))
                    finally:
                        for fh in file_handles:
                            try:
                                fh.close()
                            except Exception:
                                pass
                else:
                    resp = requests.post(url, json={"content": chunk[:2000]}, timeout=15)
                resp.raise_for_status()
                data = resp.json()
                message_id = data.get("id")
                channel_id = data.get("channel_id")
                guild_id = data.get("guild_id")
                # Remember channel/message so analytics can read reactions later. The
                # webhook response carries id + channel_id but usually omits guild_id,
                # so don't depend on the constructed URL for this.
                if message_id:
                    self.last_message_id = message_id
                if channel_id:
                    self.last_channel_id = channel_id
                if message_id and channel_id and guild_id:
                    message_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            return message_id, message_url
        except DiscordSendError:
            raise
        except requests.HTTPError as exc:
            # Discord answered with an error: this chunk was NOT posted
            status = exc.response.status_code if exc.response is not None else '?'
            body = (exc.response.text[:200] if exc.response is not None else '')
            msg = f"discord webhook HTTP {status}: {body}"
            if status == 413:
                msg += " (attachment over this server's upload limit — lower discord_upload_limit_mb)"
            logger.error(msg)
            raise DiscordSendError(msg, delivered=bool(self.last_message_id)) from None
        except (requests.ConnectionError, requests.ConnectTimeout) as exc:
            # (ReadTimeout is not a ConnectionError) — the request never got through
            msg = f"discord webhook connection failed: {type(exc).__name__}"
            logger.error(msg)
            raise DiscordSendError(msg, delivered=bool(self.last_message_id)) from None
        except Exception as exc:
            # e.g. ReadTimeout: Discord may have posted it — callers must not blindly resend
            msg = f"discord webhook send error: {type(exc).__name__}"
            logger.error(msg)
            raise DiscordSendError(msg, delivered=True if self.last_message_id else None) from None


