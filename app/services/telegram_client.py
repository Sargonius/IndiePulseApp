from __future__ import annotations

from typing import Optional, Callable, Any
import os
import mimetypes
import requests
from requests.exceptions import ConnectionError, Timeout, RequestException
import re
import time
from loguru import logger


def _retry_request(
    func: Callable[..., requests.Response],
    *args,
    max_retries: int = 3,
    backoff_base: float = 1.0,
    **kwargs
) -> requests.Response:
    """Execute a request, retrying only when the connection itself failed.

    A ReadTimeout is NOT retried: the message may already be in the channel, and
    resending would double-post. Error texts never include the request URL (it
    carries the bot token).
    """
    last_exception: Optional[Exception] = None
    for attempt in range(max_retries):
        # an aborted upload left the file handles mid-way: resend from the start
        for part in (kwargs.get('files') or {}).values():
            fh = part[1] if isinstance(part, tuple) else part
            if hasattr(fh, 'seek'):
                fh.seek(0)
        try:
            response = func(*args, **kwargs)
            if response.status_code >= 400:
                try:
                    desc = (response.json() or {}).get('description') or response.text[:200]
                except Exception:
                    desc = response.text[:200]
                raise RuntimeError(f"telegram HTTP {response.status_code}: {desc}")
            return response
        except (ConnectionError, ConnectionResetError) as e:
            if isinstance(e, requests.exceptions.ReadTimeout):
                raise RuntimeError("telegram read timeout (message may have been sent)") from None
            last_exception = RuntimeError(f"telegram connection failed: {type(e).__name__}")
            wait_time = backoff_base * (2 ** attempt)  # 1, 2, 4 seconds
            logger.warning(f"Telegram connect failed (attempt {attempt + 1}/{max_retries}): {type(e).__name__}. Retrying in {wait_time}s...")
            if attempt < max_retries - 1:
                time.sleep(wait_time)
        except Timeout:
            raise RuntimeError("telegram read timeout (message may have been sent)") from None
        except RequestException as e:
            logger.error(f"Telegram request error: {type(e).__name__}")
            raise RuntimeError(f"telegram request error: {type(e).__name__}") from None
    logger.error(f"Telegram request failed after {max_retries} attempts: {last_exception}")
    raise last_exception


def _parse_compact_number(s: str) -> Optional[int]:
    """Parse t.me compact view counts like '123', '1.2K', '3.4M'."""
    s = re.sub(r'\s+', '', (s or '')).replace(',', '.')
    if not s:
        return None
    mult = 1
    if s[-1] in ('K', 'k'):
        mult, s = 1000, s[:-1]
    elif s[-1] in ('M', 'm'):
        mult, s = 1000000, s[:-1]
    try:
        return int(float(s) * mult)
    except ValueError:
        return None


class TelegramClient:
    def __init__(self, bot_token: str) -> None:
        self.bot_token = (bot_token or '').strip()
        if not self.bot_token:
            raise ValueError('Telegram bot token is empty')

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.bot_token}/{method}"

    def _split_text(self, text: str, max_length: int) -> list[str]:
        """Split text into chunks under max_length while preserving newlines and
        paragraph breaks. Prefers to break on paragraph boundaries, then line
        breaks, then sentence ends, then spaces, falling back to a hard cut only
        when a single token is longer than max_length. Whitespace-only chunks are
        dropped so we never send an empty message."""
        if len(text) <= max_length:
            return [text] if text.strip() else []

        separators = ('\n\n', '\n', '. ', '! ', '? ', ' ')
        chunks: list[str] = []
        remaining = text
        while len(remaining) > max_length:
            window = remaining[:max_length]
            split_at = 0
            for sep in separators:
                pos = window.rfind(sep)
                if pos > 0:
                    split_at = pos + len(sep)
                    break
            if split_at <= 0:
                split_at = max_length  # no nice boundary in range; hard cut
            piece = remaining[:split_at]
            if piece.strip():
                chunks.append(piece)
            remaining = remaining[split_at:]
        if remaining.strip():
            chunks.append(remaining)

        return chunks

    def send_message(self, chat_id: str, text: str, disable_web_page_preview: bool = False) -> dict:
        """Send message, splitting into multiple messages if text exceeds 4096 chars."""
        # Split text if needed (4090 to leave some margin), preserving newlines
        chunks = self._split_text(text, 4090)

        last_response = None
        for i, chunk in enumerate(chunks):
            payload = {
                'chat_id': chat_id,
                'text': chunk,
                'disable_web_page_preview': disable_web_page_preview,
            }
            r = _retry_request(
                requests.post,
                self._api('sendMessage'),
                data=payload,
                timeout=30
            )
            last_response = r.json()
            
            # Small delay between messages to avoid rate limits
            if i < len(chunks) - 1:
                time.sleep(0.5)
        
        return last_response or {}

    def send_photo(self, chat_id: str, path: str, caption: Optional[str] = None) -> dict:
        """Send photo, splitting caption into follow-up messages if it exceeds 1024 chars."""
        # Split caption if needed, preserving newlines/formatting
        caption_text = (caption or '').strip()
        chunks = self._split_text(caption_text, 1020) if caption_text else []
        
        with open(path, 'rb') as fh:
            files = {'photo': (os.path.basename(path), fh, mimetypes.guess_type(path)[0] or 'image/jpeg')}
            data = {'chat_id': chat_id}
            if chunks:
                data['caption'] = chunks[0]
            r = _retry_request(
                requests.post,
                self._api('sendPhoto'),
                data=data,
                files=files,
                timeout=60
            )
            result = r.json()
        
        # Send remaining caption chunks as separate messages
        if len(chunks) > 1:
            time.sleep(0.3)
            for chunk in chunks[1:]:
                self.send_message(chat_id, chunk)
        
        return result

    def send_video(self, chat_id: str, path: str, caption: Optional[str] = None) -> dict:
        """Send video, splitting caption into follow-up messages if it exceeds 1024 chars."""
        # Split caption if needed, preserving newlines/formatting
        caption_text = (caption or '').strip()
        chunks = self._split_text(caption_text, 1020) if caption_text else []
        
        with open(path, 'rb') as fh:
            files = {'video': (os.path.basename(path), fh, mimetypes.guess_type(path)[0] or 'video/mp4')}
            data = {'chat_id': chat_id}
            if chunks:
                data['caption'] = chunks[0]
            r = _retry_request(
                requests.post,
                self._api('sendVideo'),
                data=data,
                files=files,
                timeout=180  # Longer timeout for video uploads
            )
            result = r.json()
        
        # Send remaining caption chunks as separate messages
        if len(chunks) > 1:
            time.sleep(0.3)
            for chunk in chunks[1:]:
                self.send_message(chat_id, chunk)
        
        return result

    def get_channel_post_views(self, chat_id: str, message_id: int) -> Optional[int]:
        """Fetch view count for a public channel post.
        Bot API doesn't expose views, so we resolve the channel username via getChat
        and parse the public t.me embed page. Returns None for private chats/groups."""
        r = _retry_request(
            requests.post,
            self._api('getChat'),
            data={'chat_id': chat_id},
            timeout=15
        )
        username = ((r.json().get('result') or {}).get('username'))
        if not username:
            return None
        resp = requests.get(
            f"https://t.me/{username}/{message_id}",
            params={'embed': '1'},
            timeout=15,
            headers={'User-Agent': 'Mozilla/5.0 (IndiePulse)'}
        )
        m = re.search(r'tgme_widget_message_views[^>]*>([^<]+)<', resp.text)
        if not m:
            return None
        return _parse_compact_number(m.group(1))

    def delete_message(self, chat_id: str, message_id: int) -> dict:
        """Delete a message from a chat."""
        payload = {
            'chat_id': chat_id,
            'message_id': message_id,
        }
        r = _retry_request(
            requests.post,
            self._api('deleteMessage'),
            data=payload,
            timeout=30
        )
        return r.json()
