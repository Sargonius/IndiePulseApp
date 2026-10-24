from __future__ import annotations

import os
import time
import requests
from loguru import logger

UPLOAD_URL = 'https://upload.twitter.com/1.1/media/upload.json'
CHUNK_BYTES = 4 * 1024 * 1024
# X drops the connection mid-upload now and then (ConnectionResetError 10054 on
# 5-40 MB clips, 2026-07 and 2026-09) while the next try goes through, so every
# step is retried. Re-sending an APPEND segment under the same index is safe.
ATTEMPTS = 3


class XMediaUploader:
	def __init__(self, oauth_token: str, oauth_token_secret: str, consumer_key: str, consumer_secret: str) -> None:
		self.oauth_token = oauth_token
		self.oauth_token_secret = oauth_token_secret
		self.consumer_key = consumer_key
		self.consumer_secret = consumer_secret

	def _oauth1_headers(self):
		from requests_oauthlib import OAuth1
		return OAuth1(self.consumer_key, self.consumer_secret, self.oauth_token, self.oauth_token_secret)

	def _call(self, method: str, step: str, **kwargs) -> requests.Response:
		"""One upload API call; network errors and 5xx are retried, other errors raise."""
		from app.services import x_billing
		for attempt in range(1, ATTEMPTS + 1):
			try:
				r = requests.request(method, UPLOAD_URL, auth=self._oauth1_headers(), **kwargs)
			except (requests.ConnectionError, requests.Timeout) as exc:
				problem = type(exc).__name__
			else:
				if r.status_code < 400:
					return r
				body = r.text[:300]
				# billed=False: we don't know that uploads are billed, so only a 402 counts
				if x_billing.remember(r.status_code, body, billed=False):
					raise RuntimeError(x_billing.ERROR_TEXT)
				if r.status_code < 500:
					raise RuntimeError(f"{step} HTTP {r.status_code}: {body}")
				problem = f"HTTP {r.status_code}"
			if attempt == ATTEMPTS:
				raise RuntimeError(f"{step} failed {ATTEMPTS} times, last: {problem}")
			logger.warning(f"x media {step}: {problem}, retry {attempt}/{ATTEMPTS - 1}")
			time.sleep(3 * attempt)

	def upload(self, file_path: str) -> str:
		# Use chunked upload for videos; simple upload for images
		import mimetypes
		ctype = mimetypes.guess_type(file_path)[0] or ''
		if ctype.startswith('video/'):
			total_bytes = os.path.getsize(file_path)
			data = self._call('POST', 'INIT', timeout=60, data={
				'command': 'INIT',
				'media_type': ctype,
				'total_bytes': total_bytes,
				'media_category': 'tweet_video',
			}).json()
			if 'media_id_string' not in data:
				raise RuntimeError(f"INIT: {data}")
			media_id = data['media_id_string']
			segment_id = 0
			with open(file_path, 'rb') as f:
				while True:
					chunk = f.read(CHUNK_BYTES)
					if not chunk:
						break
					self._call('POST', f'APPEND {segment_id}', timeout=(15, 120),
							   data={'command': 'APPEND', 'media_id': media_id, 'segment_index': segment_id},
							   files={'media': chunk})
					segment_id += 1
			fin = self._call('POST', 'FINALIZE', timeout=60, data={'command': 'FINALIZE', 'media_id': media_id}).json()
			# Poll STATUS until processing completes, or the tweet rejects the media id
			info = fin.get('processing_info') or {}
			deadline = time.time() + 180  # wait up to 3 minutes
			while (info.get('state') or '').lower() not in ('', 'succeeded'):
				if (info.get('state') or '').lower() == 'failed':
					raise RuntimeError(f"x_media_processing_failed: {info.get('error') or info}")
				wait_s = int(info.get('check_after_secs') or 2)
				if time.time() + wait_s > deadline:
					raise RuntimeError("x_media_processing_timeout")
				time.sleep(wait_s)
				js = self._call('GET', 'STATUS', timeout=30, params={'command': 'STATUS', 'media_id': media_id}).json()
				info = js.get('processing_info') or {}
			logger.info(f"x media uploaded: {os.path.basename(file_path)} {total_bytes / 1048576:.1f} MB "
						f"in {segment_id} chunks, media_id={media_id}")
			return media_id
		else:
			with open(file_path, 'rb') as f:
				blob = f.read()  # bytes, not the handle: a retry must send the whole file again
			data = self._call('POST', 'image upload', timeout=(15, 120), params={'media_category': 'tweet_image'},
							  files={'media': (os.path.basename(file_path), blob)}).json()
			if 'media_id_string' not in data:
				raise RuntimeError(str(data))
			return data['media_id_string']
