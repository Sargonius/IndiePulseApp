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
		# Single-shot for images/small videos. For large videos use chunked upload.
		import mimetypes
		ctype = mimetypes.guess_type(file_path)[0] or ''
		media_category = 'tweet_video' if ctype.startswith('video/') else 'tweet_image'
		with open(file_path, 'rb') as f:
			files = {'media': f}
			params = {'media_category': media_category}
			r = requests.post('https://upload.twitter.com/1.1/media/upload.json', files=files, params=params, auth=self._oauth1_headers(), timeout=120)
			data = r.json()
			if r.status_code >= 400 or 'media_id_string' not in data:
				raise RuntimeError(str(data))
			return data['media_id_string']


