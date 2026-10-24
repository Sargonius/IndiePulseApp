from __future__ import annotations

import httpx
from typing import Optional, Tuple
from loguru import logger


class FacebookClient:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://graph.facebook.com/v19.0"

    def create_post(self, page_id: str, message: str) -> dict:
        """Create a text post on a Page feed."""
        url = f"{self.base_url}/{page_id}/feed"
        data = {"message": message, "access_token": self.access_token}
        headers = {"Accept": "application/json"}
        logger.info(
            f"fb.create_post start page_id={page_id} msg_len={len(message or '')} url={url}"
        )
        with httpx.Client(timeout=30) as client:
            r = client.post(url, data=data, headers=headers)
            logger.info(f"fb.create_post http status={r.status_code} body={r.text[:400]}")
            r.raise_for_status()
            js = r.json()
            logger.info(f"fb.create_post ok id={(js.get('id') or js.get('post_id'))}")
            return js

    def create_photo(self, page_id: str, image_path: str, caption: str = "") -> dict:
        """Upload a photo to the Page and publish it with an optional caption."""
        url = f"{self.base_url}/{page_id}/photos"
        data = {"caption": caption, "access_token": self.access_token}
        with open(image_path, 'rb') as fh:
            files = {"source": (image_path.split('/')[-1], fh, "image/jpeg")}
            headers = {"Accept": "application/json"}
            with httpx.Client(timeout=60) as client:
                logger.info(
                    f"fb.create_photo start page_id={page_id} file={image_path} cap_len={len(caption or '')} url={url}"
                )
                r = client.post(url, data=data, files=files, headers=headers)
                logger.info(f"fb.create_photo http status={r.status_code} body={r.text[:400]}")
                r.raise_for_status()
                js = r.json()
                logger.info(f"fb.create_photo ok id={(js.get('id') or js.get('post_id'))}")
                return js

    def create_video(self, page_id: str, video_path: str, description: str = "") -> dict:
        """Upload a video to the Page (non-resumable `source` upload) and publish it.
        Returns {"id": <video id>}; the Page post appears once Facebook processes it."""
        import mimetypes
        import os
        url = f"{self.base_url}/{page_id}/videos"
        data = {"description": description, "access_token": self.access_token}
        name = os.path.basename(video_path)
        size_mb = os.path.getsize(video_path) / (1024 * 1024)
        with open(video_path, 'rb') as fh:
            files = {"source": (name, fh, mimetypes.guess_type(name)[0] or "video/mp4")}
            # the upload itself can take minutes on a slow uplink
            timeout = httpx.Timeout(connect=15, read=300, write=600, pool=30)
            with httpx.Client(timeout=timeout) as client:
                logger.info(f"fb.create_video start page_id={page_id} file={name} size={size_mb:.1f}MB "
                            f"desc_len={len(description or '')}")
                r = client.post(url, data=data, files=files, headers={"Accept": "application/json"})
                logger.info(f"fb.create_video http status={r.status_code} body={r.text[:400]}")
                if r.status_code >= 400:
                    try:
                        err = (r.json().get("error") or {}).get("message") or r.text[:300]
                    except Exception:
                        err = r.text[:300]
                    raise RuntimeError(f"fb video upload HTTP {r.status_code}: {err}")
                return r.json()

    def video_post_id(self, page_id: str, video_id: str) -> str:
        """Id of the Page post that carries the video (reactions/shares live there);
        the video id itself when Facebook doesn't expose it."""
        try:
            with httpx.Client(timeout=15) as client:
                r = client.get(f"{self.base_url}/{video_id}",
                               params={"fields": "post_id", "access_token": self.access_token})
            pid = str((r.json() or {}).get("post_id") or "") if r.status_code < 400 else ""
        except Exception:
            pid = ""
        if not pid:
            return video_id
        return pid if "_" in pid else f"{page_id}_{pid}"

    def post_to_page(self, page_id: str, message: str, media_path: Optional[str] = None) -> Tuple[str, str]:
        """
        Post text and optional media to a Facebook page.
        Returns (post_id, permalink)
        """
        url = f"{self.base_url}/{page_id}/feed"
        
        data = {
            "message": message,
            "access_token": self.access_token,
        }
        
        files = None
        if media_path:
            try:
                with open(media_path, 'rb') as f:
                    files = {"source": f}
                    data["message"] = message  # Keep message with photo
            except Exception as e:
                logger.error(f"Failed to read media file {media_path}: {e}")
                # Continue with text-only post
        
        with httpx.Client(timeout=30) as client:
            if files:
                response = client.post(url, data=data, files=files)
            else:
                response = client.post(url, data=data)
            
            response.raise_for_status()
            result = response.json()
            
            post_id = result.get("id")
            if not post_id:
                raise RuntimeError(f"Facebook API didn't return post ID: {result}")
            
            # Get permalink
            permalink = self.get_permalink(post_id)
            
            return post_id, permalink

    def get_permalink(self, post_id: str) -> str:
        """Get the permalink URL for a post"""
        url = f"{self.base_url}/{post_id}"
        params = {
            "fields": "permalink_url",
            "access_token": self.access_token,
        }
        headers = {"Accept": "application/json"}
        with httpx.Client(timeout=15) as client:
            logger.info(f"fb.get_permalink start post_id={post_id} url={url}")
            response = client.get(url, params=params, headers=headers)
            logger.info(f"fb.get_permalink http status={response.status_code} body={response.text[:400]}")
            response.raise_for_status()
            result = response.json()
            logger.info(f"fb.get_permalink ok permalink={result.get('permalink_url')}")
            permalink = result.get("permalink_url")
            if not permalink:
                raise RuntimeError(f"Facebook API didn't return permalink: {result}")
            if permalink.startswith("/"):
                permalink = "https://www.facebook.com" + permalink  # videos get a relative one

            return permalink


