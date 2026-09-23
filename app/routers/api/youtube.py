"""YouTube OAuth connect flow."""

import httpx
from app.core.config import APP_BASE_URL as _BASE_URL
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from loguru import logger
from urllib.parse import quote_plus
from app.routers.api.common import _e
from app.routers.api.common import _oauth_check_state
from app.routers.api.common import _oauth_new_state

router = APIRouter()

# YouTube OAuth (installed app style flow via user-provided tokens)
@router.post("/youtube/save")
def yt_save(
    request: Request,
    yt_client_id: str = Form(""),
    yt_client_secret: str = Form(""),
    yt_access_token: str = Form(""),
    yt_refresh_token: str = Form(""),
):
    logger.info(f"YT save received: id_len={len(yt_client_id or '')}, secret_len={len(yt_client_secret or '')}, access_len={len(yt_access_token or '')}, refresh_len={len(yt_refresh_token or '')}")
    updates = {}
    if yt_client_id.strip():
        updates["yt_client_id"] = yt_client_id.strip()
    if yt_client_secret.strip():
        updates["yt_client_secret"] = yt_client_secret.strip()
    if yt_access_token.strip():
        updates["yt_access_token"] = yt_access_token.strip()
    if yt_refresh_token.strip():
        updates["yt_refresh_token"] = yt_refresh_token.strip()
    logger.info(f"YT save writing keys: {list(updates.keys())}")
    save_settings(updates)
    logger.info("YT save done")
    # Redirect appropriately based on htmx or normal form
    if request.headers.get("HX-Request"):
        return Response(content="", headers={"HX-Redirect": "/accounts"})
    else:
        return RedirectResponse(url="/accounts", status_code=302)


# YouTube OAuth (browser consent flow; re-run it whenever Google returns
# invalid_grant — an expired/revoked refresh token)
_YT_REDIRECT_URI = f"{_BASE_URL}/api/accounts/callback/youtube"


_YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube"


@router.get("/accounts/connect/youtube")
def youtube_connect_start():
    """Return a Google consent link that yields a fresh refresh token."""
    cfg = load_settings()
    client_id = (cfg.get("yt_client_id") or "").strip()
    if not client_id:
        return HTMLResponse("<span style='color:orange'>Сначала сохрани YouTube Client ID и Secret</span>")
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={quote_plus(client_id)}&"
        f"redirect_uri={quote_plus(_YT_REDIRECT_URI)}&"
        "response_type=code&"
        f"scope={quote_plus(_YT_SCOPES)}&"
        "access_type=offline&"
        f"state={_oauth_new_state('youtube')}&"
        "prompt=consent"  # force re-issue of the refresh token
    )
    return HTMLResponse(f"<a href='{_e(auth_url)}' target='_blank'>Open Google Authorization</a>")


@router.get("/accounts/callback/youtube")
def youtube_callback(code: str = "", error: str = "", state: str = ""):
    """Exchange the consent code for access+refresh tokens and store them."""
    if error:
        return HTMLResponse(f"<span style='color:red'>Google OAuth error: {_e(error)}</span>", status_code=400)
    if not _oauth_check_state("youtube", state):
        return HTMLResponse("<span style='color:red'>Invalid OAuth state — start again from Settings → Connect YouTube</span>", status_code=400)
    cfg = load_settings()
    client_id = (cfg.get("yt_client_id") or "").strip()
    client_secret = (cfg.get("yt_client_secret") or "").strip()
    if not client_id or not client_secret:
        return HTMLResponse("<span style='color:red'>YouTube client credentials not configured</span>", status_code=400)
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(
                "https://oauth2.googleapis.com/token",
                data={
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": _YT_REDIRECT_URI,
                    "grant_type": "authorization_code",
                },
            )
            data = r.json()
        access_token = (data.get("access_token") or "").strip()
        refresh_token = (data.get("refresh_token") or "").strip()
        if not access_token:
            return HTMLResponse(f"<span style='color:red'>No access token: {_e(data)}</span>", status_code=400)
        from time import time as _now
        updates = {"yt_access_token": access_token,
                   "yt_expires_at": int(_now()) + int(data.get("expires_in") or 3600)}
        if refresh_token:
            updates["yt_refresh_token"] = refresh_token
        save_settings(updates)
        return HTMLResponse(
            "<span style='color:green'>YouTube connected! Refresh token сохранён"
            + ("" if refresh_token else " (Google не вернул refresh token — отзови доступ в myaccount.google.com/permissions и повтори)")
            + ". Можно закрыть вкладку.</span>"
        )
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>YouTube OAuth error: {_e(exc)}</span>", status_code=500)
