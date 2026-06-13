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
    """Execute a request with exponential backoff retry on connection errors."""
    last_exception = None
    
    for attempt in range(max_retries):
        try:
            response = func(*args, **kwargs)
            response.raise_for_status()
            return response
        except (ConnectionError, ConnectionResetError, Timeout) as e:
            last_exception = e
            wait_time = backoff_base * (2 ** attempt)  # 1, 2, 4 seconds
            logger.warning(
                f"Telegram request failed (attempt {attempt + 1}/{max_retries}): {type(e).__name__}: {e}. "
                f"Retrying in {wait_time}s..."
            )
            if attempt < max_retries - 1:
                time.sleep(wait_time)
        except RequestException as e:
            # For other request exceptions, log and raise immediately
            logger.error(f"Telegram request error: {type(e).__name__}: {e}")
            raise
    
    # All retries exhausted
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

    def _split_by_sentences(self, text: str, max_length: int) -> list[str]:
        """Split text by sentences, keeping each chunk under max_length."""
        if len(text) <= max_length:
            return [text]
        
        # Find sentence boundaries
        sentences = re.findall(r'[^.!?]+[.!?]+|[^.!?]+$', text)
        chunks = []
        current_chunk = ''
        
        for sentence in sentences:
            sentence = sentence.strip()
            if not sentence:
                continue
            
            # If this sentence alone exceeds max_length, split it by words
            if len(sentence) > max_length:
                words = sentence.split()
                for word in words:
                    if not current_chunk:
                        current_chunk = word
                    elif len(current_chunk) + 1 + len(word) <= max_length:
                        current_chunk += ' ' + word
                    else:
                        chunks.append(current_chunk)
                        current_chunk = word
                continue
            
            # Try to add sentence to current chunk
            if not current_chunk:
                current_chunk = sentence
            elif len(current_chunk) + 1 + len(sentence) <= max_length:
                current_chunk += ' ' + sentence
            else:
                chunks.append(current_chunk)
                current_chunk = sentence
        
        if current_chunk:
            chunks.append(current_chunk)
        
        return chunks

    def send_message(self, chat_id: str, text: str, disable_web_page_preview: bool = False) -> dict:
        """Send message, splitting into multiple messages if text exceeds 4096 chars."""
        # Split text if needed (4090 to leave some margin)
        chunks = self._split_by_sentences(text, 4090)
        
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
        # Split caption if needed
        caption_text = (caption or '').strip()
        chunks = self._split_by_sentences(caption_text, 1020) if caption_text else []
        
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
        # Split caption if needed
        caption_text = (caption or '').strip()
        chunks = self._split_by_sentences(caption_text, 1020) if caption_text else []
        
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
