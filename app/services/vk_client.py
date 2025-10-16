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

	def upload_photo_to_wall(self, group_id: str, file_path: str, token_override: str | None = None) -> str:
		from loguru import logger
		import os
		owner_id = f"-{group_id}"
		
		# Проверка что файл существует
		if not os.path.exists(file_path):
			logger.error(f"vk.upload_photo_to_wall file not found: {file_path}")
			raise RuntimeError(f"File not found: {file_path}")
		
		# 1) Get upload server
		tok = token_override or self.token
		params = {"group_id": group_id, "v": self.v, "access_token": tok}
		with httpx.Client(timeout=30) as client:
			logger.info(f"vk.photos.getWallUploadServer group_id={group_id}")
			r = client.get("https://api.vk.com/method/photos.getWallUploadServer", params=params)
			data = r.json()
			logger.info(f"vk.photos.getWallUploadServer response: {data}")
			
			if "error" in data:
				error_msg = data.get("error", {})
				logger.error(f"vk.photos.getWallUploadServer error: {error_msg}")
				raise RuntimeError(f"VK API error: {error_msg}")
			
			upload_url = (data.get("response") or {}).get("upload_url")
			if not upload_url:
				logger.error(f"vk.photos.getWallUploadServer no upload_url in response: {data}")
				raise RuntimeError("upload_url not returned by VK API")
			
			# 2) Upload file
			file_size = os.path.getsize(file_path)
			logger.info(f"vk.upload uploading file={file_path} size={file_size} bytes to {upload_url[:50]}...")
			with open(file_path, "rb") as f:
				# Detect image content type; default to image/jpeg
				import mimetypes as _m
				_ctype = (_m.guess_type(file_path)[0] or 'image/jpeg')
				u = client.post(upload_url, files={"photo": (file_path.split('/')[-1], f, _ctype)})
				u.raise_for_status()
				up = u.json()
				logger.info(f"vk.upload response: {up}")
			
			# Валидация ответа загрузки
			if not up.get("photo"):
				logger.error(f"vk.upload missing 'photo' field in response: {up}")
				raise RuntimeError(f"VK upload failed: missing 'photo' in response. Full response: {up}")
			
			# 3) Save photo
			save_params = {
				"server": up.get("server"),
				"photo": up.get("photo"),
				"hash": up.get("hash"),
				"group_id": group_id,
				"v": self.v,
				"access_token": tok,
			}
			logger.info(f"vk.photos.saveWallPhoto params keys: {list(save_params.keys())}")
			rs = client.post("https://api.vk.com/method/photos.saveWallPhoto", data=save_params)
			sd = rs.json()
			logger.info(f"vk.photos.saveWallPhoto response: {sd}")
			
			if "error" in sd:
				error_msg = sd.get("error", {})
				logger.error(f"vk.photos.saveWallPhoto error: {error_msg}")
				raise RuntimeError(f"VK saveWallPhoto error: {error_msg}")
			
			items = sd.get("response") or []
			if not items:
				logger.error(f"vk.photos.saveWallPhoto no items in response: {sd}")
				raise RuntimeError("VK saveWallPhoto: no saved items")
			
			it = items[0]
			owner = it.get("owner_id")
			mid = it.get("id")
			access_key = it.get("access_key")
			attach = f"photo{owner}_{mid}"
			if access_key:
				attach = f"{attach}_{access_key}"
			
			logger.info(f"vk.upload_photo_to_wall SUCCESS attachment={attach}")
			return attach

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


