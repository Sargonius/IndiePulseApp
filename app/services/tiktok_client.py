from __future__ import annotations

from typing import Optional
import os
import httpx
from loguru import logger


class TikTokClient:
    """TikTok Content Posting API client."""
    
    def __init__(self, access_token: str):
        self.access_token = (access_token or '').strip()
        if not self.access_token:
            raise ValueError('TikTok access token is empty')
        self.base_url = "https://open.tiktokapis.com/v2"
    
    def upload_video(
        self,
        file_path: str,
        title: str = "",
        privacy_level: str = "PUBLIC_TO_EVERYONE",
        disable_duet: bool = False,
        disable_comment: bool = False,
        disable_stitch: bool = False,
        video_cover_timestamp_ms: int = 1000,
    ) -> tuple[Optional[str], Optional[str]]:
        """
        Upload video to TikTok using Content Posting API.
        
        Process:
        1. Initialize upload and get upload URL
        2. Upload video chunks
        3. Publish the video
        
        Returns: (publish_id, share_url)
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Video file not found: {file_path}")
        
        file_size = os.path.getsize(file_path)
        
        # Step 1: Initialize upload
        try:
            init_response = self._initialize_upload(file_size)
            upload_url = init_response.get("upload_url")
            publish_id = init_response.get("publish_id")
            
            if not upload_url or not publish_id:
                raise RuntimeError(f"TikTok init failed: {init_response}")
            
            logger.info(f"TikTok upload initialized: publish_id={publish_id}")
        except Exception as exc:
            logger.error(f"TikTok init upload failed: {exc}")
            raise
        
        # Step 2: Upload video file
        try:
            self._upload_file(upload_url, file_path)
            logger.info(f"TikTok file uploaded: {file_path}")
        except Exception as exc:
            logger.error(f"TikTok file upload failed: {exc}")
            raise
        
        # Step 3: Publish video
        try:
            publish_response = self._publish_video(
                publish_id=publish_id,
                title=title,
                privacy_level=privacy_level,
                disable_duet=disable_duet,
                disable_comment=disable_comment,
                disable_stitch=disable_stitch,
                video_cover_timestamp_ms=video_cover_timestamp_ms,
            )
            
            share_url = publish_response.get("share_url")
            logger.info(f"TikTok video published: share_url={share_url}")
            
            return publish_id, share_url
        except Exception as exc:
            logger.error(f"TikTok publish failed: {exc}")
            raise
    
    def _initialize_upload(self, file_size: int) -> dict:
        """Initialize upload session."""
        url = f"{self.base_url}/post/publish/inbox/video/init/"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": file_size,  # Single chunk upload
                "total_chunk_count": 1,
            }
        }
        
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        
        # TikTok API returns nested structure
        if data.get("error", {}).get("code") != "ok":
            raise RuntimeError(f"TikTok API error: {data}")
        
        return data.get("data", {})
    
    def _upload_file(self, upload_url: str, file_path: str) -> None:
        """Upload video file to provided URL."""
        with open(file_path, 'rb') as f:
            video_data = f.read()
        
        headers = {
            "Content-Type": "video/mp4",
            "Content-Length": str(len(video_data)),
        }
        
        with httpx.Client(timeout=300) as client:
            response = client.put(upload_url, content=video_data, headers=headers)
            response.raise_for_status()
    
    def _publish_video(
        self,
        publish_id: str,
        title: str,
        privacy_level: str,
        disable_duet: bool,
        disable_comment: bool,
        disable_stitch: bool,
        video_cover_timestamp_ms: int,
    ) -> dict:
        """Publish uploaded video."""
        url = f"{self.base_url}/post/publish/video/init/"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        
        payload = {
            "publish_id": publish_id,
            "post_info": {
                "title": title[:150] if title else "New Video",
                "privacy_level": privacy_level,
                "disable_duet": disable_duet,
                "disable_comment": disable_comment,
                "disable_stitch": disable_stitch,
                "video_cover_timestamp_ms": video_cover_timestamp_ms,
            }
        }
        
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        
        if data.get("error", {}).get("code") != "ok":
            raise RuntimeError(f"TikTok publish error: {data}")
        
        return data.get("data", {})
    
    def check_status(self, publish_id: str) -> dict:
        """Check upload/publish status."""
        url = f"{self.base_url}/post/publish/status/fetch/"
        
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        
        payload = {"publish_id": publish_id}
        
        with httpx.Client(timeout=15) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        
        return data.get("data", {})

