from __future__ import annotations

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

	def reply(self, in_reply_to_id: str, text: str) -> Optional[str]:
		headers = {"Authorization": f"Bearer {self.access_token}", "Content-Type": "application/json"}
		payload = {"text": text[:280], "reply": {"in_reply_to_tweet_id": in_reply_to_id}}
		with httpx.Client(timeout=20) as client:
			r = client.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
			data = r.json()
			if r.status_code >= 400:
				raise RuntimeError(str(data))
			return (data.get("data") or {}).get("id")


