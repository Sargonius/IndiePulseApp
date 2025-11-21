from __future__ import annotations

from typing import Optional
import os
import mimetypes
import requests
import re
import time

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
			r = requests.post(self._api('sendMessage'), data=payload, timeout=20)
			r.raise_for_status()
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
			r = requests.post(self._api('sendPhoto'), data=data, files=files, timeout=30)
			r.raise_for_status()
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
			r = requests.post(self._api('sendVideo'), data=data, files=files, timeout=120)
			r.raise_for_status()
			result = r.json()
		
		# Send remaining caption chunks as separate messages
		if len(chunks) > 1:
			time.sleep(0.3)
			for chunk in chunks[1:]:
				self.send_message(chat_id, chunk)
		
		return result
