from __future__ import annotations

import os
from typing import Optional
import requests


class XMediaUploader:
	def __init__(self, oauth_token: str, oauth_token_secret: str, consumer_key: str, consumer_secret: str) -> None:
		self.oauth_token = oauth_token
		self.oauth_token_secret = oauth_token_secret
		self.consumer_key = consumer_key
		self.consumer_secret = consumer_secret

	def _oauth1_headers(self):
		from requests_oauthlib import OAuth1
		return OAuth1(self.consumer_key, self.consumer_secret, self.oauth_token, self.oauth_token_secret)

	def upload(self, file_path: str) -> str:
		# Use chunked upload for videos; simple upload for images
		import mimetypes
		ctype = mimetypes.guess_type(file_path)[0] or ''
		if ctype.startswith('video/'):
			# INIT
			total_bytes = os.path.getsize(file_path)
			init_params = {
				'command': 'INIT',
				'media_type': ctype,
				'total_bytes': total_bytes,
				'media_category': 'tweet_video',
			}
			r = requests.post('https://upload.twitter.com/1.1/media/upload.json', data=init_params, auth=self._oauth1_headers(), timeout=60)
			data = r.json()
			if r.status_code >= 400 or 'media_id_string' not in data:
				raise RuntimeError(str(data))
			media_id = data['media_id_string']
			# APPEND
			segment_id = 0
			with open(file_path, 'rb') as f:
				while True:
					chunk = f.read(4 * 1024 * 1024)
					if not chunk:
						break
					files = {'media': chunk}
					params = {'command': 'APPEND', 'media_id': media_id, 'segment_index': segment_id}
					r = requests.post('https://upload.twitter.com/1.1/media/upload.json', data=params, files=files, auth=self._oauth1_headers(), timeout=120)
					if r.status_code >= 400:
						raise RuntimeError(str(r.text))
					segment_id += 1
			# FINALIZE
			fin_params = {'command': 'FINALIZE', 'media_id': media_id}
			r = requests.post('https://upload.twitter.com/1.1/media/upload.json', data=fin_params, auth=self._oauth1_headers(), timeout=60)
			if r.status_code >= 400:
				raise RuntimeError(str(r.text))
			# Poll STATUS for processing completion to avoid invalid media_ids on tweet
			try:
				import time
				status_params = {'command': 'STATUS', 'media_id': media_id}
				deadline = time.time() + 180  # wait up to 3 minutes
				while True:
					rs = requests.get('https://upload.twitter.com/1.1/media/upload.json', params=status_params, auth=self._oauth1_headers(), timeout=30)
					js = rs.json()
					info = js.get('processing_info') or {}
					state = (info.get('state') or '').lower()
					if state in ('', 'succeeded'):
						break
					if state == 'failed':
						raise RuntimeError(f"x_media_processing_failed: {info}")
					# Backoff according to API hint
					wait_s = int(info.get('check_after_secs') or 2)
					if time.time() + wait_s > deadline:
						raise RuntimeError("x_media_processing_timeout")
					time.sleep(wait_s)
			except Exception as _exc:
				# Propagate status errors to caller for clearer diagnostics
				raise
			return media_id
		else:
			with open(file_path, 'rb') as f:
				files = {'media': f}
				params = {'media_category': 'tweet_image'}
				r = requests.post('https://upload.twitter.com/1.1/media/upload.json', files=files, params=params, auth=self._oauth1_headers(), timeout=120)
				data = r.json()
				if r.status_code >= 400 or 'media_id_string' not in data:
					raise RuntimeError(str(data))
				return data['media_id_string']


