from __future__ import annotations

from typing import Optional
import os
import mimetypes
import requests

class TelegramClient:
	def __init__(self, bot_token: str) -> None:
		self.bot_token = (bot_token or '').strip()
		if not self.bot_token:
			raise ValueError('Telegram bot token is empty')

	def _api(self, method: str) -> str:
		return f"https://api.telegram.org/bot{self.bot_token}/{method}"

	def send_message(self, chat_id: str, text: str, disable_web_page_preview: bool = False) -> dict:
		payload = {
			'chat_id': chat_id,
			'text': text[:4096],
			'disable_web_page_preview': disable_web_page_preview,
		}
		r = requests.post(self._api('sendMessage'), data=payload, timeout=20)
		r.raise_for_status()
		return r.json()

	def send_photo(self, chat_id: str, path: str, caption: Optional[str] = None) -> dict:
		with open(path, 'rb') as fh:
			files = {'photo': (os.path.basename(path), fh, mimetypes.guess_type(path)[0] or 'image/jpeg')}
			data = {'chat_id': chat_id}
			if caption:
				data['caption'] = caption[:1024]
			r = requests.post(self._api('sendPhoto'), data=data, files=files, timeout=30)
			r.raise_for_status()
			return r.json()

	def send_video(self, chat_id: str, path: str, caption: Optional[str] = None) -> dict:
		with open(path, 'rb') as fh:
			files = {'video': (os.path.basename(path), fh, mimetypes.guess_type(path)[0] or 'video/mp4')}
			data = {'chat_id': chat_id}
			if caption:
				data['caption'] = caption[:1024]
			r = requests.post(self._api('sendVideo'), data=data, files=files, timeout=120)
			r.raise_for_status()
			return r.json()
