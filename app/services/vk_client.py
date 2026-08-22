from __future__ import annotations

import os
import time as _time
from typing import Callable, Optional, TypeVar

import httpx
from loguru import logger


# VK's upload servers and api.vk.com are frequently slow to connect; a short
# 20-30s total timeout was the main cause of "почти 100%" publish failures that
# succeed on a manual re-run. Use a generous per-phase timeout instead.
_VK_TIMEOUT = httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)

# Transient network errors that are safe to retry (the request either never left
# or the server never answered). We deliberately do NOT retry wall.post on these
# except pure connect failures — see _post_wall.
_TRANSIENT = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)
_CONNECT_ONLY = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

T = TypeVar("T")


def _retry(fn: Callable[[], T], *, attempts: int = 2, on=(_TRANSIENT), base_delay: float = 1.5) -> T:
    """Call fn(), retrying on transient network errors up to `attempts` times."""
    last: Optional[BaseException] = None
    for i in range(attempts):
        try:
            return fn()
        except on as exc:  # type: ignore[misc]
            last = exc
            logger.warning(f"VK transient error (attempt {i + 1}/{attempts}): {exc!r}")
            if i + 1 < attempts:
                _time.sleep(base_delay * (i + 1))
    assert last is not None
    raise last


class VKClient:
    def __init__(self, access_token: str, api_version: str = "5.199") -> None:
        self.token = access_token
        self.v = api_version

    def wall_post(self, owner_id: str, message: str) -> str:
        params = {"owner_id": owner_id, "message": message, "from_group": 1, "v": self.v, "access_token": self.token}

        def _do() -> str:
            with httpx.Client(timeout=_VK_TIMEOUT) as client:
                r = client.post("https://api.vk.com/method/wall.post", data=params)
                data = r.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return str((data.get("response") or {}).get("post_id"))

        # Only retry when the connection never established (safe — the post was
        # definitely not created). A read-timeout could mean the post DID land.
        return _retry(_do, on=_CONNECT_ONLY)

    def upload_photo_to_wall(self, group_id: str, file_path: str, token_override: str | None = None) -> str:
        owner_id = f"-{group_id}"

        if not os.path.exists(file_path):
            logger.error(f"vk.upload_photo_to_wall file not found: {file_path}")
            raise RuntimeError(f"File not found: {file_path}")

        tok = token_override or self.token
        with httpx.Client(timeout=_VK_TIMEOUT) as client:
            # 1) Get upload server (idempotent — safe to retry)
            def _get_server() -> dict:
                params = {"group_id": group_id, "v": self.v, "access_token": tok}
                logger.info(f"vk.photos.getWallUploadServer group_id={group_id}")
                r = client.get("https://api.vk.com/method/photos.getWallUploadServer", params=params)
                return r.json()

            data = _retry(_get_server)
            logger.info(f"vk.photos.getWallUploadServer response: {data}")
            if "error" in data:
                logger.error(f"vk.photos.getWallUploadServer error: {data.get('error', {})}")
                raise RuntimeError(f"VK API error: {data.get('error', {})}")
            upload_url = (data.get("response") or {}).get("upload_url")
            if not upload_url:
                raise RuntimeError("upload_url not returned by VK API")

            # 2) Upload file (reopen the file each attempt so retries re-send it)
            file_size = os.path.getsize(file_path)
            import mimetypes as _m
            _ctype = (_m.guess_type(file_path)[0] or "image/jpeg")
            logger.info(f"vk.upload uploading file={file_path} size={file_size} bytes")

            def _upload() -> dict:
                with open(file_path, "rb") as f:
                    u = client.post(upload_url, files={"photo": (file_path.split("/")[-1], f, _ctype)})
                    u.raise_for_status()
                    return u.json()

            up = _retry(_upload)
            logger.info(f"vk.upload response: {up}")
            if not up.get("photo"):
                raise RuntimeError(f"VK upload failed: missing 'photo' in response. Full response: {up}")

            # 3) Save photo (idempotent w.r.t. the wall — safe to retry)
            def _save() -> dict:
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
                return rs.json()

            sd = _retry(_save)
            logger.info(f"vk.photos.saveWallPhoto response: {sd}")
            if "error" in sd:
                logger.error(f"vk.photos.saveWallPhoto error: {sd.get('error', {})}")
                raise RuntimeError(f"VK saveWallPhoto error: {sd.get('error', {})}")

            items = sd.get("response") or []
            if not items:
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

        def _do() -> dict:
            with httpx.Client(timeout=_VK_TIMEOUT) as client:
                r = client.get("https://api.vk.com/method/video.save", params=params)
                data = r.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return data.get("response") or {}

        return _retry(_do)

    def upload_video(self, upload_url: str, file_path: str) -> dict:
        with httpx.Client(timeout=None) as client:
            with open(file_path, "rb") as f:
                r = client.post(upload_url, files={"video_file": (file_path.split('/')[-1], f, 'video/mp4')})
                r.raise_for_status()
                return r.json()

    def wall_post_with_video(self, owner_id: str, message: str, video_attachment: str) -> str:
        params = {"owner_id": owner_id, "message": message, "from_group": 1, "attachments": video_attachment, "v": self.v, "access_token": self.token}

        def _do() -> str:
            with httpx.Client(timeout=_VK_TIMEOUT) as client:
                r = client.post("https://api.vk.com/method/wall.post", data=params)
                data = r.json()
                if "error" in data:
                    raise RuntimeError(str(data["error"]))
                return str((data.get("response") or {}).get("post_id"))

        return _retry(_do, on=_CONNECT_ONLY)
