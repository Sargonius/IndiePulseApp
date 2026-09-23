"""TikTok OAuth connect flow and token test."""

import httpx
from app.core.config import APP_BASE_URL as _BASE_URL
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from urllib.parse import quote_plus
from app.routers.api.common import _e
from app.routers.api.common import _oauth_check_state
from app.routers.api.common import _oauth_new_state

router = APIRouter()

@router.get("/accounts/connect/tiktok")
def tiktok_connect_start():
    """Start TikTok OAuth flow (inbox/draft mode → only video.upload scope)."""
    cfg = load_settings()
    client_key = (cfg.get("tiktok_client_key") or "").strip()
    if not client_key:
        return HTMLResponse("<span style='color:orange'>Сначала сохрани TikTok Client Key ниже</span>")

    redirect_uri = (cfg.get("tiktok_redirect_uri") or "").strip() or f"{_BASE_URL}/api/accounts/callback/tiktok"
    # NOTE: no video.publish here — that scope needs a passed TikTok app audit
    # and requesting it for an unaudited app fails the whole authorize screen.
    scope = "user.info.basic,video.upload"

    auth_url = (
        f"https://www.tiktok.com/v2/auth/authorize?"
        f"client_key={quote_plus(client_key)}&"
        f"response_type=code&"
        f"scope={quote_plus(scope)}&"
        f"redirect_uri={quote_plus(redirect_uri)}&"
        f"state={_oauth_new_state('tiktok')}"
    )

    return HTMLResponse(f"<a href='{_e(auth_url)}' target='_blank'>Open TikTok Authorization</a>")


@router.get("/accounts/callback/tiktok")
def tiktok_callback(code: str, state: str = ""):
    """Handle TikTok OAuth callback."""
    cfg = load_settings()
    client_key = cfg.get("tiktok_client_key", "")
    client_secret = cfg.get("tiktok_client_secret", "")

    if not client_key or not client_secret:
        return HTMLResponse("<span style='color:red'>TikTok credentials not configured</span>", status_code=400)

    if not _oauth_check_state("tiktok", state):
        return HTMLResponse("<span style='color:red'>Invalid OAuth state</span>", status_code=400)

    # Exchange code for access token
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    redirect_uri = (cfg.get("tiktok_redirect_uri") or "").strip() or f"{_BASE_URL}/api/accounts/callback/tiktok"
    
    payload = {
        "client_key": client_key,
        "client_secret": client_secret,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post(token_url, data=payload, headers=headers)
            r.raise_for_status()
            data = r.json()
        
        # TikTok returns access_token, refresh_token, expires_in
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token", "")
        expires_in = int(data.get("expires_in", 0))
        
        if not access_token:
            return HTMLResponse(f"<span style='color:red'>No access token: {_e(data)}</span>", status_code=400)
        
        from time import time as _now
        save_settings({
            "tiktok_access_token": access_token,
            "tiktok_refresh_token": refresh_token,
            "tiktok_token_expires_at": int(_now()) + expires_in,
        })
        
        return HTMLResponse("<span style='color:green'>TikTok connected successfully!</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>TikTok OAuth error: {_e(str(exc))}</span>", status_code=500)


@router.post("/tiktok/test")
def tiktok_test():
    """Test TikTok API connection (check token validity)."""
    from app.services.tiktok_client import get_valid_access_token
    cfg = load_settings()
    access_token = get_valid_access_token(cfg)

    if not access_token:
        return HTMLResponse("<span style='color:orange'>TikTok not connected</span>")
    
    # Test API call - get user info
    try:
        url = "https://open.tiktokapis.com/v2/user/info/"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        
        with httpx.Client(timeout=15) as client:
            r = client.get(url, headers=headers, params={"fields": "open_id,union_id,avatar_url"})
            data = r.json()
        
        if r.status_code >= 400 or data.get("error", {}).get("code") != "ok":
            return HTMLResponse(f"<span style='color:red'>TikTok API error: {_e(data)}</span>")
        
        user_info = data.get("data", {}).get("user", {})
        return HTMLResponse(f"<span style='color:green'>TikTok connected. User: {_e(user_info.get('open_id', 'unknown'))}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>TikTok test failed: {_e(str(exc))}</span>")
