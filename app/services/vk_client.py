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


