from __future__ import annotations

import httpx
from typing import Optional, Tuple
from loguru import logger


class FacebookClient:
    def __init__(self, access_token: str):
        self.access_token = access_token
        self.base_url = "https://graph.facebook.com/v18.0"

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
        
        with httpx.Client(timeout=15) as client:
            response = client.get(url, params=params)
            response.raise_for_status()
            result = response.json()
            
            permalink = result.get("permalink_url")
            if not permalink:
                raise RuntimeError(f"Facebook API didn't return permalink: {result}")
            
            return permalink


