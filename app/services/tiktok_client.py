from __future__ import annotations

from typing import Optional
import os
import httpx
from loguru import logger


# TikTok chunk rules: each chunk 5-64 MB; a video < 5 MB must be uploaded whole;
# the final chunk may exceed chunk_size (up to 128 MB) to absorb trailing bytes.
_MAX_CHUNK = 64 * 1024 * 1024  # 64 MB


def get_valid_access_token(cfg: dict, *, margin_sec: int = 300) -> str:
    """Locked wrapper: the refresh token rotates on every refresh, so two processes
    refreshing at once would leave one of them holding a dead token."""
    from time import time as _now
    access = (cfg.get('tiktok_access_token') or '').strip()
    try:
        expires_at = int(cfg.get('tiktok_token_expires_at') or 0)
    except Exception:
        expires_at = 0
    if access and expires_at and _now() < expires_at - margin_sec:
        return access  # fast path, no lock needed
    from app.core.filelock import FileLock
    from app.services.settings import load_settings
    with FileLock('tiktok_refresh'):
        return _get_valid_access_token(load_settings(), margin_sec=margin_sec)


def _get_valid_access_token(cfg: dict, *, margin_sec: int = 300) -> str:
    """Return a usable TikTok access token, refreshing it when (nearly) expired.

    TikTok access tokens live 24h; the refresh token (~1 year) ROTATES on every
    refresh, so the new pair is persisted back to settings immediately.
    Returns '' when TikTok is not connected at all. An unknown expiry (0) is
    treated as expired so legacy tokens get refreshed on first use.
    """
    from time import time as _now
    from app.services.settings import save_settings

    access = (cfg.get('tiktok_access_token') or '').strip()
    refresh = (cfg.get('tiktok_refresh_token') or '').strip()
    try:
        expires_at = int(cfg.get('tiktok_token_expires_at') or 0)
    except Exception:
        expires_at = 0
    if not access and not refresh:
        return ''
    if access and expires_at and _now() < expires_at - margin_sec:
        return access
    if not refresh:
        return access  # can't refresh; let the API call fail loudly
    client_key = (cfg.get('tiktok_client_key') or '').strip()
    client_secret = (cfg.get('tiktok_client_secret') or '').strip()
    if not client_key or not client_secret:
        logger.warning('TikTok token expired but client key/secret are missing — cannot refresh')
        return access
    try:
        resp = httpx.post(
            'https://open.tiktokapis.com/v2/oauth/token/',
            data={
                'client_key': client_key,
                'client_secret': client_secret,
                'grant_type': 'refresh_token',
                'refresh_token': refresh,
            },
            headers={'Content-Type': 'application/x-www-form-urlencoded'},
            timeout=20,
        )
        data = resp.json()
    except Exception as exc:
        logger.error(f'TikTok token refresh request failed: {exc}')
        return access
    new_access = (data.get('access_token') or '').strip()
    if not new_access:
        logger.error(f'TikTok token refresh rejected: {data}')
        return access
    update = {
        'tiktok_access_token': new_access,
        'tiktok_refresh_token': (data.get('refresh_token') or refresh),
        'tiktok_token_expires_at': int(_now()) + int(data.get('expires_in') or 86400),
    }
    if data.get('scope'):
        update['tiktok_scope'] = str(data['scope'])
    save_settings(update)
    logger.info('TikTok access token refreshed')
    return new_access


class TikTokAPIError(RuntimeError):
    """A TikTok API answer with an error code; `code` is TikTok's (scope_not_authorized...)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code


def _data(r: httpx.Response, what: str) -> dict:
    """The `data` of a TikTok v2 answer; TikTokAPIError when `error.code` isn't "ok"."""
    try:
        body = r.json()
    except ValueError:
        raise TikTokAPIError(f"http_{r.status_code}", f"{what}: {r.text[:200]}")
    err = (body.get("error") or {}) if isinstance(body, dict) else {}
    code = str(err.get("code") or ("ok" if r.status_code < 400 else f"http_{r.status_code}"))
    if code != "ok":
        raise TikTokAPIError(code, str(err.get("message") or "")[:300])
    return (body.get("data") or {}) if isinstance(body, dict) else {}


class TikTokClient:
    """TikTok Content Posting API client — *inbox / draft* mode.

    We upload the finished vertical video to the creator's TikTok inbox. The
    creator then opens the TikTok app, adds a native/trending sound, writes the
    caption and taps publish. This flow works without passing the TikTok app
    audit (unlike public Direct Post) and tends to get better reach because the
    API cannot attach TikTok's native sounds.

    Flow (2 steps, no publish call):
      1. POST /post/publish/inbox/video/init/  -> {publish_id, upload_url}
      2. PUT the file bytes to upload_url with a Content-Range header
    """

    def __init__(self, access_token: str):
        self.access_token = (access_token or '').strip()
        if not self.access_token:
            raise ValueError('TikTok access token is empty')
        self.base_url = "https://open.tiktokapis.com/v2"

    # ------------------------------------------------------------------ inbox
    def upload_to_inbox(self, file_path: str, title: str = "") -> tuple[Optional[str], Optional[str]]:
        """Upload a video to the creator's TikTok inbox (draft).

        The `title` is accepted for signature-compatibility but is NOT sent:
        in inbox mode the caption is written by the creator inside the app.

        Returns: (publish_id, None). There is no public share_url yet — the post
        does not exist publicly until the creator finishes it in the app.
        """
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Video file not found: {file_path}")

        file_size = os.path.getsize(file_path)
        if file_size <= 0:
            raise RuntimeError(f"TikTok upload: empty file {file_path}")

        chunk_size, total_chunks = self._plan_chunks(file_size)

        # Step 1 — initialize inbox upload
        init = self._init_inbox(file_size, chunk_size, total_chunks)
        upload_url = init.get("upload_url")
        publish_id = init.get("publish_id")
        if not upload_url or not publish_id:
            raise RuntimeError(f"TikTok inbox init failed: {init}")
        logger.info(f"TikTok inbox upload initialized: publish_id={publish_id} size={file_size} chunks={total_chunks}")

        # Step 2 — upload the bytes (chunked, with Content-Range)
        self._upload_chunks(upload_url, file_path, file_size, chunk_size, total_chunks)
        logger.info(f"TikTok inbox upload complete: publish_id={publish_id}")

        # No publish step: the video now sits in the creator's inbox as a draft.
        return publish_id, None

    @staticmethod
    def _plan_chunks(file_size: int) -> tuple[int, int]:
        """Return (chunk_size, total_chunk_count) obeying TikTok's chunk rules."""
        if file_size <= _MAX_CHUNK:
            # Single chunk (also covers files < 5 MB, which must be sent whole).
            return file_size, 1
        chunk_size = _MAX_CHUNK
        total_chunks = file_size // _MAX_CHUNK  # last chunk absorbs the remainder
        if total_chunks <= 1:
            return file_size, 1
        return chunk_size, total_chunks

    def _init_inbox(self, file_size: int, chunk_size: int, total_chunks: int) -> dict:
        url = f"{self.base_url}/post/publish/inbox/video/init/"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "source_info": {
                "source": "FILE_UPLOAD",
                "video_size": file_size,
                "chunk_size": chunk_size,
                "total_chunk_count": total_chunks,
            }
        }
        with httpx.Client(timeout=30) as client:
            response = client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        # TikTok wraps errors as {"error": {"code": "ok"|...}}
        if (data.get("error", {}) or {}).get("code") not in ("ok", None, ""):
            raise RuntimeError(f"TikTok inbox init error: {data}")
        return data.get("data", {}) or {}

    def _upload_chunks(self, upload_url: str, file_path: str, file_size: int, chunk_size: int, total_chunks: int) -> None:
        with open(file_path, 'rb') as f:
            for i in range(total_chunks):
                start = i * chunk_size
                # The final chunk runs to EOF (it may exceed chunk_size).
                end = (file_size - 1) if i == total_chunks - 1 else (start + chunk_size - 1)
                f.seek(start)
                data = f.read(end - start + 1)
                headers = {
                    "Content-Type": "video/mp4",
                    "Content-Length": str(len(data)),
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                }
                with httpx.Client(timeout=300) as client:
                    response = client.put(upload_url, content=data, headers=headers)
                    response.raise_for_status()

    # ------------------------------------------------------------------ status
    def check_status(self, publish_id: str) -> dict:
        """Upload/publish status of a publish_id (video.upload scope, 30 calls a minute):
        {status: PROCESSING_UPLOAD | SEND_TO_USER_INBOX | PUBLISH_COMPLETE | FAILED,
        fail_reason, publicaly_available_post_id: [...] (TikTok's spelling)}. For an
        inbox draft PUBLISH_COMPLETE means the creator posted it; the post id follows
        once the post is public and has passed moderation."""
        return self._post("/post/publish/status/fetch/", {}, {"publish_id": publish_id})

    # ------------------------------------------------------- account and stats
    def _get(self, path: str, params: dict) -> dict:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{self.base_url}{path}", params=params,
                           headers={"Authorization": f"Bearer {self.access_token}"})
        return _data(r, path)

    def _post(self, path: str, params: dict, body: dict) -> dict:
        with httpx.Client(timeout=20) as client:
            r = client.post(f"{self.base_url}{path}", params=params, json=body,
                            headers={"Authorization": f"Bearer {self.access_token}",
                                     "Content-Type": "application/json; charset=UTF-8"})
        return _data(r, path)

    def user_info(self, fields: list[str]) -> dict:
        """GET /user/info/ — each field needs its scope (follower_count: user.info.stats,
        username: user.info.profile); asking for one without it fails the whole call."""
        return (self._get("/user/info/", {"fields": ",".join(fields)}).get("user") or {})

    def query_videos(self, video_ids: list[str], fields: list[str]) -> list[dict]:
        """POST /video/query/ (video.list scope), at most 20 ids per call."""
        out: list[dict] = []
        ids = [str(v) for v in video_ids if str(v or "").strip()]
        for i in range(0, len(ids), 20):
            data = self._post("/video/query/", {"fields": ",".join(fields)},
                              {"filters": {"video_ids": ids[i:i + 20]}})
            out += data.get("videos") or []
        return out

    # -------------------------------------------------- backwards-compat shim
    def upload_video(self, file_path: str, title: str = "", privacy_level: str = "", **_ignored) -> tuple[Optional[str], Optional[str]]:
        """Deprecated: old direct-post signature. Now routes to inbox upload.

        `privacy_level` is ignored — public Direct Post requires a passed TikTok
        app audit (see `direct_post_public`).
        """
        logger.warning("TikTokClient.upload_video is deprecated; using inbox upload instead")
        return self.upload_to_inbox(file_path, title=title)

    def direct_post_public(self, *_args, **_kwargs):
        """Public Direct Post (SELF_ONLY until audited). Not enabled yet.

        Requires the `video.publish` scope AND a passed TikTok app audit. Left as
        a stub behind the future `tiktok_direct_post` feature flag.
        """
        raise NotImplementedError(
            "TikTok public Direct Post requires a passed app audit. "
            "Use upload_to_inbox (draft) until the audit is approved."
        )
