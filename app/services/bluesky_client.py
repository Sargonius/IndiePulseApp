from __future__ import annotations

import io
import os
import re
from datetime import datetime, timezone
from typing import Optional, Tuple

import httpx
from loguru import logger

# Bluesky hard limits
MAX_POST_GRAPHEMES = 300
MAX_IMAGE_BYTES = 976 * 1024  # ~1MB blob limit, keep margin
MAX_IMAGES_PER_POST = 4


class BlueskyClient:
    """Minimal AT Protocol client for posting to Bluesky.

    Auth is dead simple: handle + app password (created at
    bsky.app -> Settings -> App Passwords). No OAuth dance.
    """

    def __init__(self, identifier: str, app_password: str, service: str = "https://bsky.social"):
        self.identifier = (identifier or '').strip().lstrip('@')
        self.app_password = (app_password or '').strip()
        self.service = (service or 'https://bsky.social').strip().rstrip('/')
        if not self.identifier or not self.app_password:
            raise ValueError('Bluesky handle or app password is empty')
        self.access_jwt: Optional[str] = None
        self.did: Optional[str] = None
        self.handle: Optional[str] = None

    # ── auth ────────────────────────────────────────────────────────────────
    def login(self) -> dict:
        url = f"{self.service}/xrpc/com.atproto.server.createSession"
        with httpx.Client(timeout=20) as client:
            r = client.post(url, json={"identifier": self.identifier, "password": self.app_password})
            logger.info(f"bsky.login status={r.status_code}")
            r.raise_for_status()
            data = r.json()
        self.access_jwt = data.get('accessJwt')
        self.did = data.get('did')
        self.handle = data.get('handle') or self.identifier
        if not (self.access_jwt and self.did):
            raise RuntimeError(f"Bluesky login failed: {data}")
        return data

    def _ensure_session(self) -> None:
        if not self.access_jwt:
            self.login()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_jwt}"}

    # ── media ───────────────────────────────────────────────────────────────
    def upload_image(self, path: str) -> Optional[dict]:
        """Upload an image blob, compressing to fit the ~1MB limit.
        Returns an app.bsky.embed.images image dict, or None on failure."""
        self._ensure_session()
        try:
            data, mime, width, height = self._prepare_image(path)
        except Exception as exc:
            logger.error(f"bsky.upload_image prepare failed {path}: {exc}")
            return None
        url = f"{self.service}/xrpc/com.atproto.repo.uploadBlob"
        headers = self._headers()
        headers["Content-Type"] = mime
        with httpx.Client(timeout=60) as client:
            r = client.post(url, content=data, headers=headers)
            logger.info(f"bsky.upload_blob status={r.status_code} size={len(data)}")
            r.raise_for_status()
            blob = (r.json() or {}).get('blob')
        if not blob:
            return None
        image = {"alt": "", "image": blob}
        if width and height:
            image["aspectRatio"] = {"width": width, "height": height}
        return image

    @staticmethod
    def _prepare_image(path: str) -> Tuple[bytes, str, int, int]:
        """Read an image; re-encode as JPEG progressively smaller if over the blob limit."""
        import mimetypes
        mime = mimetypes.guess_type(path)[0] or 'image/jpeg'
        with open(path, 'rb') as fh:
            raw = fh.read()
        width = height = 0
        try:
            from PIL import Image
            with Image.open(io.BytesIO(raw)) as img:
                width, height = img.size
        except Exception:
            pass
        if len(raw) <= MAX_IMAGE_BYTES and mime in ('image/jpeg', 'image/png', 'image/webp', 'image/gif'):
            return raw, mime, width, height
        # Re-encode with Pillow, shrinking until it fits
        from PIL import Image
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        width, height = img.size
        for quality in (90, 85, 75, 65):
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=quality, optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue(), 'image/jpeg', img.size[0], img.size[1]
        # Still too big: downscale by half until it fits
        while min(img.size) > 320:
            img = img.resize((img.size[0] // 2, img.size[1] // 2))
            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=80, optimize=True)
            if buf.tell() <= MAX_IMAGE_BYTES:
                return buf.getvalue(), 'image/jpeg', img.size[0], img.size[1]
        raise RuntimeError(f"image too large for Bluesky: {path}")

    # ── rich text facets (links + hashtags) ─────────────────────────────────
    @staticmethod
    def _facets_for(text: str) -> list[dict]:
        facets: list[dict] = []

        def byte_off(char_idx: int) -> int:
            return len(text[:char_idx].encode('utf-8'))

        for m in re.finditer(r'https?://[^\s<>"\')\]]+', text):
            facets.append({
                "index": {"byteStart": byte_off(m.start()), "byteEnd": byte_off(m.end())},
                "features": [{"$type": "app.bsky.richtext.facet#link", "uri": m.group(0)}],
            })
        for m in re.finditer(r'(?<!\w)#(\w+)', text, re.UNICODE):
            facets.append({
                "index": {"byteStart": byte_off(m.start()), "byteEnd": byte_off(m.end())},
                "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}],
            })
        return facets

    # ── posting ──────────────────────────────────────────────────────────────
    def post(
        self,
        text: str,
        image_paths: Optional[list[str]] = None,
        reply_root: Optional[dict] = None,
        reply_parent: Optional[dict] = None,
        langs: Optional[list[str]] = None,
    ) -> Tuple[str, str, str]:
        """Create a single post. Returns (uri, cid, web_url)."""
        self._ensure_session()
        record: dict = {
            "$type": "app.bsky.feed.post",
            "text": text,
            "createdAt": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        }
        if langs:
            record["langs"] = langs
        facets = self._facets_for(text)
        if facets:
            record["facets"] = facets
        images = []
        for p in (image_paths or [])[:MAX_IMAGES_PER_POST]:
            img = self.upload_image(p)
            if img:
                images.append(img)
        if images:
            record["embed"] = {"$type": "app.bsky.embed.images", "images": images}
        if reply_parent and reply_root:
            record["reply"] = {"root": reply_root, "parent": reply_parent}
        url = f"{self.service}/xrpc/com.atproto.repo.createRecord"
        payload = {"repo": self.did, "collection": "app.bsky.feed.post", "record": record}
        with httpx.Client(timeout=30) as client:
            r = client.post(url, json=payload, headers=self._headers())
            logger.info(f"bsky.post status={r.status_code} body={r.text[:300]}")
            r.raise_for_status()
            data = r.json()
        uri = data.get('uri') or ''
        cid = data.get('cid') or ''
        rkey = uri.rsplit('/', 1)[-1] if uri else ''
        web_url = f"https://bsky.app/profile/{self.handle}/post/{rkey}" if rkey else ''
        return uri, cid, web_url

    def post_thread(
        self,
        text: str,
        hashtags: str = '',
        image_paths: Optional[list[str]] = None,
        langs: Optional[list[str]] = None,
    ) -> Tuple[str, str]:
        """Post text (split into a reply thread if over 300 chars) with optional
        images on the first post. Hashtags are kept in the first post.
        Returns (first_uri, first_web_url)."""
        body = (text or '').strip()
        tags = (hashtags or '').strip()
        suffix = ('\n' + tags) if tags else ''
        budget = max(1, MAX_POST_GRAPHEMES - len(suffix))
        chunks = _sentence_chunks(body, budget) or ['']
        first_text = (chunks[0] + suffix).strip() or tags
        rest = chunks[1:]

        uri, cid, web_url = self.post(first_text, image_paths=image_paths, langs=langs)
        root = {"uri": uri, "cid": cid}
        parent = root
        for chunk in rest:
            for seg in _sentence_chunks(chunk, MAX_POST_GRAPHEMES):
                p_uri, p_cid, _ = self.post(seg, reply_root=root, reply_parent=parent, langs=langs)
                parent = {"uri": p_uri, "cid": p_cid}
        return uri, web_url

    # ── metrics ──────────────────────────────────────────────────────────────
    @staticmethod
    def get_post_metrics(uri: str) -> dict:
        """Fetch public engagement counts for a post (no auth needed)."""
        url = "https://public.api.bsky.app/xrpc/app.bsky.feed.getPosts"
        with httpx.Client(timeout=15) as client:
            r = client.get(url, params={"uris": uri})
            r.raise_for_status()
            data = r.json()
        posts = data.get('posts') or []
        if not posts:
            return {}
        p = posts[0]
        return {
            "like": p.get('likeCount'),
            "repost": p.get('repostCount'),
            "reply": p.get('replyCount'),
            "quote": p.get('quoteCount'),
        }


def _sentence_chunks(text: str, limit: int) -> list[str]:
    """Split text on sentence boundaries into chunks not exceeding limit chars.
    Over-long sentences fall back to word splitting."""
    text = (text or '').strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    parts = re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", text)
    chunks: list[str] = []
    buf = ''
    for s in parts:
        s = s.strip()
        if not s:
            continue
        if len(s) > limit:
            # flush buffer, then split the long sentence by words
            if buf:
                chunks.append(buf)
                buf = ''
            for word in s.split():
                if not buf:
                    buf = word
                elif len(buf) + 1 + len(word) <= limit:
                    buf += ' ' + word
                else:
                    chunks.append(buf)
                    buf = word
            continue
        if not buf:
            buf = s
        elif len(buf) + 1 + len(s) <= limit:
            buf += ' ' + s
        else:
            chunks.append(buf)
            buf = s
    if buf:
        chunks.append(buf)
    return chunks
