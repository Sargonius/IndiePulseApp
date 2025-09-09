from __future__ import annotations

from typing import Optional

import httpx


class VKClient:
	def __init__(self, access_token: str, api_version: str = "5.199") -> None:
		self.token = access_token
		self.v = api_version

	def wall_post(self, owner_id: str, message: str) -> str:
		params = {"owner_id": owner_id, "message": message, "from_group": 1, "v": self.v, "access_token": self.token}
		with httpx.Client(timeout=20) as client:
			r = client.post("https://api.vk.com/method/wall.post", data=params)
			data = r.json()
			if "error" in data:
				raise RuntimeError(str(data["error"]))
			return str((data.get("response") or {}).get("post_id"))

	def upload_photo_to_wall(self, group_id: str, file_path: str) -> str:
		owner_id = f"-{group_id}"
		# 1) Get upload server
		params = {"group_id": group_id, "v": self.v, "access_token": self.token}
		with httpx.Client(timeout=30) as client:
			r = client.get("https://api.vk.com/method/photos.getWallUploadServer", params=params)
			data = r.json()
			if "error" in data:
				raise RuntimeError(str(data["error"]))
			upload_url = (data.get("response") or {}).get("upload_url")
			if not upload_url:
				raise RuntimeError("upload_url not returned")
			# 2) Upload file
			with open(file_path, "rb") as f:
				u = client.post(upload_url, files={"photo": (file_path.split('/')[-1], f, 'image/jpeg')})
				u.raise_for_status()
				up = u.json()
			# 3) Save photo
			save_params = {
				"server": up.get("server"),
				"photo": up.get("photo"),
				"hash": up.get("hash"),
				"group_id": group_id,
				"v": self.v,
				"access_token": self.token,
			}
			rs = client.post("https://api.vk.com/method/photos.saveWallPhoto", data=save_params)
			sd = rs.json()
			if "error" in sd:
				raise RuntimeError(str(sd["error"]))
			items = sd.get("response") or []
			if not items:
				raise RuntimeError("no saved items")
			it = items[0]
			owner = it.get("owner_id")
			mid = it.get("id")
			return f"photo{owner}_{mid}"

	def get_video_upload_server(self, group_id: str, name: str, token_override: str | None = None) -> dict:
		tok = token_override or self.token
		params = {"group_id": group_id, "name": name, "wallpost": 0, "v": self.v, "access_token": tok}
		with httpx.Client(timeout=60) as client:
			r = client.get("https://api.vk.com/method/video.save", params=params)
			data = r.json()
			if "error" in data:
				raise RuntimeError(str(data["error"]))
			return data.get("response") or {}

	def upload_video(self, upload_url: str, file_path: str) -> dict:
		with httpx.Client(timeout=None) as client:
			with open(file_path, "rb") as f:
				r = client.post(upload_url, files={"video_file": (file_path.split('/')[-1], f, 'video/mp4')})
				r.raise_for_status()
				return r.json()

	def wall_post_with_video(self, owner_id: str, message: str, video_attachment: str) -> str:
		params = {"owner_id": owner_id, "message": message, "from_group": 1, "attachments": video_attachment, "v": self.v, "access_token": self.token}
		with httpx.Client(timeout=30) as client:
			r = client.post("https://api.vk.com/method/wall.post", data=params)
			data = r.json()
			if "error" in data:
				raise RuntimeError(str(data["error"]))
			return str((data.get("response") or {}).get("post_id"))


