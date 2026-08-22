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


class DiscordClient:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url.strip()
        # Populated after a successful send so callers can read reactions later.
        self.last_message_id: Optional[str] = None
        self.last_channel_id: Optional[str] = None

    async def send_message_async(self, content: str, file_path: Optional[str] = None, file_paths: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
        """Send a message to the webhook. Returns (message_id, message_url)."""
        if not self.webhook_url:
            raise ValueError("Discord webhook URL is empty")
        url = self.webhook_url
        if "?" in url:
            url = url + "&wait=true"
        else:
            url = url + "?wait=true"
        async with httpx.AsyncClient(timeout=30) as client:
            chunks = _split_by_sentences(content or "", 2000)
            sources = (file_paths or [])
            if not sources and file_path:
                sources = [file_path]
            has_files = any(p and os.path.exists(p) for p in sources)
            message_id = None
            message_url = None
            for i, chunk in enumerate(chunks):
                with_files = has_files and i == 0
                if with_files:
                    try:
                        payload = {"content": chunk[:2000]}
                        file_handles = []
                        try:
                            files_multi = []
                            # Determine per-file size limit from settings
                            try:
                                cfg = load_settings()
                                max_mb = int(cfg.get("discord_max_upload_mb") or 8)
                            except Exception:
                                max_mb = 8
                            max_bytes = max_mb * 1024 * 1024
                            for p in sources:
                                if not (p and os.path.exists(p)):
                                    continue
                                size = os.path.getsize(p)
                                if size > max_bytes:
                                    continue
                                filename = os.path.basename(p)
                                ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                                fh = open(p, 'rb')
                                file_handles.append(fh)
                                files_multi.append(('file', (filename, fh, ctype)))
                            if files_multi:
                                first_name = files_multi[0][1][0]
                                first_ctype = mimetypes.guess_type(first_name)[0] or 'application/octet-stream'
                                if first_ctype.startswith('image/'):
                                    payload["embeds"] = [{"image": {"url": f"attachment://{first_name}"}}]
                            if files_multi:
                                resp = await client.post(url, data={"payload_json": json.dumps(payload)}, files=files_multi)
                            else:
                                resp = await client.post(url, json=payload)
                        finally:
                            for fh in file_handles:
                                try:
                                    fh.close()
                                except Exception:
                                    pass
                    except Exception:
                        payload = {"content": (chunk[:1800] + "\n[Attachment upload failed]")}
                        resp = await client.post(url, json=payload)
                else:
                    payload = {"content": chunk[:2000]}
                    resp = await client.post(url, json=payload)
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

    def send_message(self, content: str, file_path: Optional[str] = None, file_paths: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
        # Use synchronous requests for reliability with multipart + embed
        if not self.webhook_url:
            raise ValueError("Discord webhook URL is empty")
        url = self.webhook_url + ("&wait=true" if "?" in self.webhook_url else "?wait=true")
        try:
            sources = (file_paths or [])
            if not sources and file_path:
                sources = [file_path]
            has_files = any(p and os.path.exists(p) for p in sources)
            chunks = _split_by_sentences(content or "", 2000)
            message_id = None
            message_url = None
            for i, chunk in enumerate(chunks):
                if has_files and i == 0:
                    payload = {"content": chunk[:2000]}
                    files_multi = []
                    file_handles = []
                    try:
                        try:
                            cfg = load_settings()
                            max_mb = int(cfg.get("discord_max_upload_mb") or 8)
                        except Exception:
                            max_mb = 8
                        max_bytes = max_mb * 1024 * 1024
                        for p in sources:
                            if not (p and os.path.exists(p)):
                                continue
                            size = os.path.getsize(p)
                            if size > max_bytes:
                                continue
                            filename = os.path.basename(p)
                            ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                            fh = open(p, 'rb')
                            file_handles.append(fh)
                            files_multi.append(('file', (filename, fh, ctype)))
                        if files_multi:
                            first_name = files_multi[0][1][0]
                            first_ctype = mimetypes.guess_type(first_name)[0] or 'application/octet-stream'
                            if first_ctype.startswith('image/'):
                                payload["embeds"] = [{"image": {"url": f"attachment://{first_name}"}}]
                            resp = requests.post(url, data={"payload_json": json.dumps(payload)}, files=files_multi, timeout=60)
                        else:
                            resp = requests.post(url, json=payload, timeout=15)
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
        except Exception as exc:
            logger.error(f"discord webhook send error: {exc}")
            raise


