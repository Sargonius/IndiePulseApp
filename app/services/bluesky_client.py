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
VIDEO_SERVICE = "https://video.bsky.app"
VIDEO_PROCESSING_TIMEOUT_S = 300


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
        self.did_doc: dict = {}

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
        self.did_doc = data.get('didDoc') or {}
        if not (self.access_jwt and self.did):
            raise RuntimeError(f"Bluesky login failed: {data}")
        return data

    def _ensure_session(self) -> None:
        if not self.access_jwt:
            self.login()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.access_jwt}"}

    # ── media ───────────────────────────────────────────────────────────────
    def upload_image(self, path: str) -> dict:
        """Upload an image blob, compressing to fit the ~1MB limit.
        Returns an app.bsky.embed.images image dict; raises on failure (a post
        with media must not go out without it)."""
        self._ensure_session()
        data, mime, width, height = self._prepare_image(path)
        url = f"{self.service}/xrpc/com.atproto.repo.uploadBlob"
        headers = self._headers()
        headers["Content-Type"] = mime
        with httpx.Client(timeout=60) as client:
            r = client.post(url, content=data, headers=headers)
            logger.info(f"bsky.upload_blob status={r.status_code} size={len(data)}")
            if r.status_code >= 400:
                raise RuntimeError(f"bluesky image upload HTTP {r.status_code}: {r.text[:300]}")
            blob = (r.json() or {}).get('blob')
        if not blob:
            raise RuntimeError(f"bluesky image upload returned no blob: {os.path.basename(path)}")
        image = {"alt": "", "image": blob}
        if width and height:
            image["aspectRatio"] = {"width": width, "height": height}
        return image

    def _pds_host(self) -> str:
        """Host of the account's own PDS: the video service uploads the blob there, so the
        service token's audience is did:web:<that host> (bsky.social is only the entryway)."""
        from urllib.parse import urlparse
        for svc in (self.did_doc.get('service') or []):
            if str(svc.get('id', '')).endswith('#atproto_pds') or svc.get('type') == 'AtprotoPersonalDataServer':
                host = urlparse(str(svc.get('serviceEndpoint') or '')).hostname
                if host:
                    return host
        if (self.did or '').startswith('did:plc:'):
            with httpx.Client(timeout=15) as client:
                r = client.get(f"https://plc.directory/{self.did}")
            if r.status_code < 400:
                for svc in (r.json() or {}).get('service') or []:
                    if str(svc.get('id', '')).endswith('#atproto_pds'):
                        return urlparse(str(svc.get('serviceEndpoint') or '')).hostname or ''
        return urlparse(self.service).hostname or ''

    def upload_video(self, path: str) -> dict:
        """Upload a video through Bluesky's video service and wait until it is processed.
        Returns an app.bsky.embed.video embed; raises on any failure."""
        import mimetypes
        import time
        self._ensure_session()
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{self.service}/xrpc/com.atproto.server.getServiceAuth", headers=self._headers(),
                           params={"aud": f"did:web:{self._pds_host()}", "lxm": "com.atproto.repo.uploadBlob",
                                   "exp": int(time.time()) + 30 * 60})
        if r.status_code >= 400:
            raise RuntimeError(f"bluesky video auth HTTP {r.status_code}: {r.text[:300]}")
        video_auth = {"Authorization": f"Bearer {(r.json() or {}).get('token')}"}
        name = os.path.basename(path)
        with open(path, 'rb') as fh:
            data = fh.read()
        timeout = httpx.Timeout(connect=15, read=120, write=600, pool=30)
        with httpx.Client(timeout=timeout) as client:
            r = client.post(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.uploadVideo", params={"did": self.did, "name": name},
                            content=data,
                            headers={**video_auth, "Content-Type": mimetypes.guess_type(name)[0] or "video/mp4"})
        logger.info(f"bsky.video upload status={r.status_code} size={len(data)} body={r.text[:300]}")
        try:
            body = r.json() or {}
        except Exception:
            body = {}
        job = body.get('jobStatus') or body
        # 409 already_exists: this exact file was processed before; the job still has the blob
        if r.status_code >= 400 and not (r.status_code == 409 and job.get('jobId')):
            raise RuntimeError(f"bluesky video upload HTTP {r.status_code}: {r.text[:300]}")
        deadline = time.time() + VIDEO_PROCESSING_TIMEOUT_S
        with httpx.Client(timeout=20) as client:
            while not job.get('blob'):
                if job.get('state') == 'JOB_STATE_FAILED':
                    raise RuntimeError(f"bluesky video processing failed: {job.get('error') or ''} "
                                       f"{job.get('message') or ''}".strip())
                if not job.get('jobId'):
                    raise RuntimeError(f"bluesky video upload returned no job: {str(body)[:300]}")
                if time.time() > deadline:
                    raise RuntimeError(f"bluesky video processing timeout ({job.get('state')}, {job.get('progress')}%)")
                time.sleep(2)
                r = client.get(f"{VIDEO_SERVICE}/xrpc/app.bsky.video.getJobStatus",
                               params={"jobId": job['jobId']}, headers=video_auth)
                if r.status_code >= 400:
                    raise RuntimeError(f"bluesky video status HTTP {r.status_code}: {r.text[:300]}")
                job = (r.json() or {}).get('jobStatus') or {}
        embed = {"$type": "app.bsky.embed.video", "video": job['blob']}
        try:
            from app.services.video_editor import _probe_video_info
            info = _probe_video_info(path)
            if info.get('width') and info.get('height'):
                embed["aspectRatio"] = {"width": info['width'], "height": info['height']}
        except Exception:
            pass
        return embed

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
        video_path: Optional[str] = None,
    ) -> Tuple[str, str, str]:
        """Create a single post (a video replaces images: one embed per post).
        Returns (uri, cid, web_url)."""
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
        if video_path:
            record["embed"] = self.upload_video(video_path)
        else:
            images = [self.upload_image(p) for p in (image_paths or [])[:MAX_IMAGES_PER_POST]]
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
        video_path: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Post text (split into a reply thread if over 300 chars) with optional
        images or a video on the first post. Hashtags are kept in the first post.
        Returns (first_uri, first_web_url)."""
        body = (text or '').strip()
        tags = (hashtags or '').strip()
        suffix = ('\n' + tags) if tags else ''
        budget = max(1, MAX_POST_GRAPHEMES - len(suffix))
        chunks = _sentence_chunks(body, budget) or ['']
        first_text = (chunks[0] + suffix).strip() or tags
        rest = chunks[1:]

        uri, cid, web_url = self.post(first_text, image_paths=image_paths, langs=langs, video_path=video_path)
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
