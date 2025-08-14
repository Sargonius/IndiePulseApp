from __future__ import annotations

import os
from typing import Optional

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials


class YouTubeClient:
    def __init__(self, access_token: str, refresh_token: Optional[str] = None, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> None:
        self.creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=["https://www.googleapis.com/auth/youtube.upload"],
        )
        self.youtube = build("youtube", "v3", credentials=self.creds)

    def upload_short(self, file_path: str, title: str, description: str = "", tags: Optional[list[str]] = None, privacy_status: str = "public") -> tuple[Optional[str], Optional[str]]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:4900],
                "tags": tags or [],
                "categoryId": "24",
            },
            "status": {"privacyStatus": privacy_status},
        }
        media = MediaFileUpload(file_path, chunksize=-1, resumable=True)
        request = self.youtube.videos().insert(part=",".join(body.keys()), body=body, media_body=media)
        response = None
        while response is None:
            status, response = request.next_chunk()
            if status is None:
                continue
        vid = response.get("id") if isinstance(response, dict) else None
        url = f"https://www.youtube.com/watch?v={vid}" if vid else None
        return vid, url


