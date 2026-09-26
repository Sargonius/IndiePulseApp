"""OAuth access-token helpers shared by the web app and the worker.

Both processes may refresh at the same moment. X refresh tokens are single-use
(each refresh invalidates the previous one), so refreshes run under a cross-process
lock and re-read settings inside it: whoever comes second picks up the token the
first one already saved instead of burning the rotated refresh token.
"""
from __future__ import annotations

import base64
from time import time as _now

import httpx
from loguru import logger

from app.core.filelock import FileLock
from app.services.settings import load_settings, save_settings


def x_ensure_token(force: bool = False) -> str:
    """Return a valid X OAuth2 access token, refreshing if expiring. Raises on failure."""
    cfg = load_settings()
    token = (cfg.get("x_access_token") or "").strip()
    exp_ts = int(cfg.get("x_expires_at_ts") or 0)
    if token and not force and not (exp_ts and exp_ts - int(_now()) < 60):
        return token
    with FileLock("x_refresh"):
        cfg = load_settings()  # another process may have refreshed while we waited
        token = (cfg.get("x_access_token") or "").strip()
        exp_ts = int(cfg.get("x_expires_at_ts") or 0)
        if token and not force and not (exp_ts and exp_ts - int(_now()) < 60):
            return token
        refresh = (cfg.get("x_refresh_token") or "").strip()
        client_id = (cfg.get("x_client_id") or "").strip()
        client_secret = (cfg.get("x_client_secret") or "").strip()
        if not (refresh and client_id and client_secret):
            raise RuntimeError("x_refresh_unavailable")
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        with httpx.Client(timeout=15) as client:
            r = client.post(
                "https://api.twitter.com/2/oauth2/token",
                data={"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id},
                headers={"Content-Type": "application/x-www-form-urlencoded", "Authorization": f"Basic {basic}"},
            )
            jd = r.json()
        if r.status_code >= 400 or not jd.get("access_token"):
            raise RuntimeError(f"x_refresh_failed: {jd}")
        expires_in = int(jd.get("expires_in", 0))
        save_settings({
            "x_access_token": jd["access_token"],
            "x_refresh_token": jd.get("refresh_token", refresh),
            "x_token_type": jd.get("token_type", cfg.get("x_token_type")),
            "x_expires_at": expires_in,
            "x_expires_at_ts": int(_now()) + expires_in,
        })
        logger.info("X access token refreshed")
        return jd["access_token"]


class YouTubeAuthError(RuntimeError):
    """Refresh failed; `.reauth` is True when the user must re-run Connect YouTube."""

    def __init__(self, msg: str, reauth: bool) -> None:
        super().__init__(msg)
        self.reauth = reauth


def _remember_yt_auth_error(error: str) -> None:
    """Keep the last refresh failure that needs a reconnect in settings, so the
    Accounts checklist and the global alert can show it ('' = working again).
    Written only on change: metrics hit this every 30 minutes."""
    if (load_settings().get("yt_auth_error") or "") == error:
        return
    save_settings({"yt_auth_error": error, "yt_auth_error_at": int(_now()) if error else 0})


def yt_access_token(force: bool = False) -> str:
    """Return a fresh YouTube access token (1h lifetime), refreshing and persisting it.

    Google's client library refreshes in memory only, so without this the stored
    token was stale for every direct REST call (metrics) after the first hour.
    """
    cfg = load_settings()
    token = (cfg.get("yt_access_token") or "").strip()
    exp = int(cfg.get("yt_expires_at") or 0)
    if token and not force and exp and exp - int(_now()) > 120:
        return token
    with FileLock("yt_refresh"):
        cfg = load_settings()
        token = (cfg.get("yt_access_token") or "").strip()
        exp = int(cfg.get("yt_expires_at") or 0)
        if token and not force and exp and exp - int(_now()) > 120:
            return token
        refresh = (cfg.get("yt_refresh_token") or "").strip()
        cid = (cfg.get("yt_client_id") or "").strip()
        csec = (cfg.get("yt_client_secret") or "").strip()
        if not (refresh and cid and csec):
            raise YouTubeAuthError("yt_refresh_unavailable: no refresh token or client id/secret", reauth=True)
        with httpx.Client(timeout=20) as client:
            r = client.post("https://oauth2.googleapis.com/token", data={
                "grant_type": "refresh_token", "refresh_token": refresh,
                "client_id": cid, "client_secret": csec,
            })
            d = r.json()
        if r.status_code >= 400 or not d.get("access_token"):
            err = d.get("error") or f"http_{r.status_code}"
            exc = YouTubeAuthError(f"yt_refresh_failed: {err} {d.get('error_description') or ''}".strip(),
                                   reauth=err in ("invalid_grant", "invalid_client", "unauthorized_client"))
            if exc.reauth:
                _remember_yt_auth_error(str(exc))
            raise exc
        save_settings({
            "yt_access_token": d["access_token"],
            "yt_expires_at": int(_now()) + int(d.get("expires_in") or 3600),
            **({"yt_auth_error": "", "yt_auth_error_at": 0} if cfg.get("yt_auth_error") else {}),
        })
        return d["access_token"]
