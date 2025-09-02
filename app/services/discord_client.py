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
            if (file_paths and any(os.path.exists(p) for p in file_paths)) or (file_path and os.path.exists(file_path)):
                try:
                    # Build files list (support multiple). Discord limit ~25MB per file
                    payload = {"content": content[:2000]}
                    file_handles = []
                    try:
                        files_multi = []
                        sources = (file_paths or [])
                        if not sources and file_path:
                            sources = [file_path]
                        for p in sources:
                            if not (p and os.path.exists(p)): continue
                            size = os.path.getsize(p)
                            if size > 24 * 1024 * 1024:
                                continue
                            filename = os.path.basename(p)
                            ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                            fh = open(p, 'rb')
                            file_handles.append(fh)
                            files_multi.append(('file', (filename, fh, ctype)))
                        # Embed only first image if any
                        if files_multi:
                            first_name = files_multi[0][1][0]
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

    def send_message(self, content: str, file_path: Optional[str] = None, file_paths: Optional[list[str]] = None) -> tuple[Optional[str], Optional[str]]:
        # Use synchronous requests for reliability with multipart + embed
        if not self.webhook_url:
            raise ValueError("Discord webhook URL is empty")
        url = self.webhook_url + ("&wait=true" if "?" in self.webhook_url else "?wait=true")
        try:
            sources = (file_paths or [])
            if not sources and file_path:
                sources = [file_path]
            # If there are any files, attach them all in one message
            if any(p and os.path.exists(p) for p in sources):
                payload = {"content": content[:2000]}
                files_multi = []
                file_handles = []
                try:
                    for p in sources:
                        if not (p and os.path.exists(p)):
                            continue
                        size = os.path.getsize(p)
                        if size > 24 * 1024 * 1024:
                            continue
                        filename = os.path.basename(p)
                        ctype = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
                        fh = open(p, 'rb')
                        file_handles.append(fh)
                        files_multi.append(('file', (filename, fh, ctype)))
                    if files_multi:
                        first_name = files_multi[0][1][0]
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


