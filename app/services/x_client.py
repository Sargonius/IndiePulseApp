from __future__ import annotations

import os
from typing import Optional

import httpx


class XClient:
	def __init__(self, access_token: str) -> None:
		self.access_token = access_token.strip()

	def tweet(self, text: str) -> tuple[Optional[str], Optional[str]]:
		headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
		payload = {"text": text[:280]}
		with httpx.Client(timeout=20) as client:
			r = client.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
			data = r.json()
			if r.status_code >= 400:
				raise RuntimeError(str(data))
			tid = (data.get("data") or {}).get("id")
			url = f"https://twitter.com/i/web/status/{tid}" if tid else None
			return tid, url


