from __future__ import annotations

import os
from typing import Optional, Tuple

from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials


def _clip_utf8_bytes(text: str, max_bytes: int) -> str:
    """Trim text so its UTF-8 form fits max_bytes without splitting a character.

    The Data API limits snippet.description by BYTES (5000), not characters —
    Japanese/CJK text is ~3 bytes per char, so a naive [:N] char slice overflows.
    """
    raw = (text or "").encode("utf-8")
    if len(raw) <= max_bytes:
        return text or ""
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _sanitize_yt_text(text: str) -> str:
    # The API rejects '<' and '>' anywhere in title/description.
    return (text or "").replace("<", "‹").replace(">", "›")


class YouTubeClient:
    def __init__(self, access_token: str, refresh_token: Optional[str] = None, client_id: Optional[str] = None, client_secret: Optional[str] = None) -> None:
        self.creds = Credentials(
            token=access_token,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            # Include broader scope to allow playlist management in addition to upload
            scopes=[
                "https://www.googleapis.com/auth/youtube.upload",
                "https://www.googleapis.com/auth/youtube",
            ],
        )
        self.youtube = build("youtube", "v3", credentials=self.creds)

    def upload_short(self, file_path: str, title: str, description: str = "", tags: Optional[list[str]] = None, privacy_status: str = "public") -> tuple[Optional[str], Optional[str]]:
        if not os.path.exists(file_path):
            raise FileNotFoundError(file_path)
        body = {
            "snippet": {
                "title": _sanitize_yt_text(title)[:100],
                "description": _clip_utf8_bytes(_sanitize_yt_text(description), 4900),
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

    # Generic upload (alias to upload_short for now)
    def upload_video(self, file_path: str, title: str, description: str = "", tags: Optional[list[str]] = None, privacy_status: str = "public") -> Tuple[Optional[str], Optional[str]]:
        return self.upload_short(file_path, title=title, description=description, tags=tags, privacy_status=privacy_status)

    def ensure_playlist(self, title: str) -> Optional[str]:
        """Return playlist ID for given title, creating it if necessary.
        Requires youtube scope; if not permitted, returns None.
        """
        try:
            # Try to find existing playlist by title
            req = self.youtube.playlists().list(part="snippet", mine=True, maxResults=50)
            while req is not None:
                resp = req.execute()
                for item in (resp.get("items") or []):
                    if ((item.get("snippet") or {}).get("title") or "") == title:
                        return item.get("id")
                req = self.youtube.playlists().list_next(req, resp)
            # Create if not found
            body = {
                "snippet": {"title": title[:150], "description": "Auto playlist"},
                "status": {"privacyStatus": "public"},
            }
            created = self.youtube.playlists().insert(part=",".join(body.keys()), body=body).execute()
            return created.get("id")
        except Exception:
            return None

    def add_to_playlist(self, playlist_id: str, video_id: str) -> bool:
        try:
            body = {
                "snippet": {
                    "playlistId": playlist_id,
                    "resourceId": {"kind": "youtube#video", "videoId": video_id},
                }
            }
            self.youtube.playlistItems().insert(part="snippet", body=body).execute()
            return True
        except Exception:
            return False


