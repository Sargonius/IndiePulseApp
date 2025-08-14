from __future__ import annotations

import json
from typing import Optional
import os
import mimetypes

import httpx
import requests
from loguru import logger


class DiscordClient:
    def __init__(self, webhook_url: str) -> None:
        self.webhook_url = webhook_url.strip()

    async def send_message_async(self, content: str, file_path: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        """Send a message to the webhook. Returns (message_id, message_url)."""
        if not self.webhook_url:
            raise ValueError("Discord webhook URL is empty")
        url = self.webhook_url
        if "?" in url:
            url = url + "&wait=true"
        else:
            url = url + "?wait=true"
        async with httpx.AsyncClient(timeout=30) as client:
            if file_path and os.path.exists(file_path):
                try:
                    size = os.path.getsize(file_path)
                    # Discord hard-limit ~25MB per attachment
                    if size > 24 * 1024 * 1024:
                        payload = {"content": (content[:1800] + "\n[Attachment skipped: >24MB]")}
                        resp = await client.post(url, json=payload)
                    else:
                        filename = os.path.basename(file_path)
                        ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                        with open(file_path, 'rb') as fh:
                            # Preferred: single file field with embed referencing attachment://filename
                            files = {'file': (filename, fh, ctype)}
                            payload = {
                                "content": content[:2000],
                                "embeds": [
                                    {"image": {"url": f"attachment://{filename}"}}
                                ],
                            }
                            data = {"payload_json": json.dumps(payload)}
                            resp = await client.post(url, data=data, files=files)
                            if resp.status_code >= 400:
                                # Fallback to basic content only
                                fh.seek(0)
                                resp = await client.post(url, data={"content": content[:2000]}, files={'file': (filename, fh, ctype)})
                except Exception:
                    # Fallback to plain text on any upload error
                    payload = {"content": (content[:1800] + "\n[Attachment upload failed]")}
                    resp = await client.post(url, json=payload)
            else:
                payload = {"content": content[:2000]}
                resp = await client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            message_id = data.get("id")
            channel_id = data.get("channel_id")
            guild_id = data.get("guild_id")
            message_url = None
            if message_id and channel_id and guild_id:
                message_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            return message_id, message_url

    def send_message(self, content: str, file_path: Optional[str] = None) -> tuple[Optional[str], Optional[str]]:
        # Use synchronous requests for reliability with multipart + embed
        if not self.webhook_url:
            raise ValueError("Discord webhook URL is empty")
        url = self.webhook_url + ("&wait=true" if "?" in self.webhook_url else "?wait=true")
        try:
            if file_path and os.path.exists(file_path):
                size = os.path.getsize(file_path)
                if size > 24 * 1024 * 1024:
                    resp = requests.post(url, json={"content": (content[:1800] + "\n[Attachment skipped: >24MB]")}, timeout=30)
                else:
                    filename = os.path.basename(file_path)
                    ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                    with open(file_path, 'rb') as fh:
                        files = {'file': (filename, fh, ctype)}
                        payload = {"content": content[:2000], "embeds": [{"image": {"url": f"attachment://{filename}"}}]}
                        resp = requests.post(url, data={"payload_json": json.dumps(payload)}, files=files, timeout=30)
            else:
                resp = requests.post(url, json={"content": content[:2000]}, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            message_id = data.get("id")
            channel_id = data.get("channel_id")
            guild_id = data.get("guild_id")
            message_url = None
            if message_id and channel_id and guild_id:
                message_url = f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
            return message_id, message_url
        except Exception as exc:
            logger.error(f"discord webhook send error: {exc}")
            raise


