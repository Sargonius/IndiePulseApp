from __future__ import annotations

from typing import Optional
import os
import time
import httpx
from loguru import logger


GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v21.0"


class InstagramClient:
    """Publish Instagram Reels via the Graph API using the *resumable* upload
    protocol (rupload.facebook.com), so no public video URL is required — we
    upload the local file directly.

    Flow:
      1. POST /{ig_user_id}/media  (media_type=REELS, upload_type=resumable) -> {id, uri}
      2. POST the file bytes to the returned `uri`  (Authorization: OAuth, offset, file_size)
      3. GET /{container_id}?fields=status_code  until FINISHED
      4. POST /{ig_user_id}/media_publish  (creation_id)

    Requires the `instagram_business_content_publish` permission (Meta app review)
    and a Page access token for the Page linked to the IG business account.
    """

    def __init__(self, ig_user_id: str, access_token: str, version: str = DEFAULT_VERSION):
        self.ig_user_id = (ig_user_id or '').strip()
        self.access_token = (access_token or '').strip()
        self.version = (version or DEFAULT_VERSION).strip()
        if not self.ig_user_id or not self.access_token:
            raise ValueError('Instagram not configured (ig_user_id / ig_access_token)')

    def upload_reel(
        self,
        file_path: str,
        caption: str = "",
        share_to_feed: bool = True,
        poll_timeout: int = 240,
        poll_interval: int = 4,
    ) -> tuple[Optional[str], Optional[str]]:
        """Upload + publish a Reel. Returns (media_id, permalink)."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Video file not found: {file_path}")

        container_id, upload_uri = self._create_container(caption, share_to_feed)
        logger.info(f"IG reels container created: id={container_id}")
        self._upload_file(upload_uri, file_path)
        logger.info(f"IG reels file uploaded: {file_path}")
        self._wait_finished(container_id, timeout=poll_timeout, interval=poll_interval)
        media_id = self._publish(container_id)
        logger.info(f"IG reels published: media_id={media_id}")
        return media_id, self._permalink(media_id)

    def _create_container(self, caption: str, share_to_feed: bool) -> tuple[str, str]:
        url = f"{GRAPH}/{self.version}/{self.ig_user_id}/media"
        data = {
            "media_type": "REELS",
            "upload_type": "resumable",
            "caption": caption or "",
            "share_to_feed": "true" if share_to_feed else "false",
            "access_token": self.access_token,
        }
        with httpx.Client(timeout=30) as client:
            r = client.post(url, data=data)
            d = r.json()
        if r.status_code >= 400 or "error" in d:
            raise RuntimeError(f"ig_container_error: {d}")
        cid = d.get("id")
        if not cid:
            raise RuntimeError(f"ig_container_no_id: {d}")
        uri = d.get("uri") or f"https://rupload.facebook.com/ig-api-upload/{self.version}/{cid}"
        return cid, uri

    def _upload_file(self, upload_uri: str, file_path: str) -> None:
        file_size = os.path.getsize(file_path)
        headers = {
            "Authorization": f"OAuth {self.access_token}",
            "offset": "0",
            "file_size": str(file_size),
            "Content-Type": "application/octet-stream",
        }
        with open(file_path, "rb") as f:
            data = f.read()
        with httpx.Client(timeout=600) as client:
            r = client.post(upload_uri, content=data, headers=headers)
            try:
                d = r.json()
            except Exception:
                d = {"raw": (r.text or "")[:300]}
        ok = bool(d.get("success") in (True, "true", "True"))
        if r.status_code >= 400 or not ok:
            raise RuntimeError(f"ig_upload_error status={r.status_code} body={d}")

    def _wait_finished(self, container_id: str, timeout: int = 240, interval: int = 4) -> None:
        url = f"{GRAPH}/{self.version}/{container_id}"
        waited = 0
        last = ""
        while waited <= timeout:
            with httpx.Client(timeout=20) as client:
                r = client.get(url, params={"fields": "status_code", "access_token": self.access_token})
                d = r.json()
            last = (d.get("status_code") or "").upper()
            if last == "FINISHED":
                return
            if last == "ERROR":
                raise RuntimeError(f"ig_container_error_status: {d}")
            time.sleep(interval)
            waited += interval
        raise RuntimeError(f"ig_container_timeout after {timeout}s (last status={last})")

    def _publish(self, container_id: str) -> Optional[str]:
        url = f"{GRAPH}/{self.version}/{self.ig_user_id}/media_publish"
        with httpx.Client(timeout=30) as client:
            r = client.post(url, data={"creation_id": container_id, "access_token": self.access_token})
            d = r.json()
        if r.status_code >= 400 or "error" in d:
            raise RuntimeError(f"ig_publish_error: {d}")
        return d.get("id")

    def _permalink(self, media_id: Optional[str]) -> Optional[str]:
        if not media_id:
            return None
        try:
            url = f"{GRAPH}/{self.version}/{media_id}"
            with httpx.Client(timeout=20) as client:
                r = client.get(url, params={"fields": "permalink", "access_token": self.access_token})
                d = r.json()
            return d.get("permalink")
        except Exception:
            return None
