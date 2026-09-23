"""X (Twitter) OAuth2 PKCE connect flow and test tweet."""

import base64
import hashlib
import httpx
import secrets
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi.responses import HTMLResponse
from urllib.parse import quote_plus
from app.routers.api.common import _e
from app.routers.api.common import _oauth_check_state
from app.routers.api.common import _oauth_new_state

router = APIRouter()

def _pkce_pair():
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


@router.get("/accounts/connect/x")
def x_connect_start():
    cfg = load_settings()
    client_id = (cfg.get("x_client_id") or "").strip()
    redirect_uri = (cfg.get("x_redirect_uri") or "").strip()
    if not client_id or not redirect_uri:
        return HTMLResponse("<span style='color:orange'>Укажите X Client ID и Redirect URI</span>")
    verifier, challenge = _pkce_pair()
    save_settings({"x_code_verifier": verifier})
    scope = "tweet.read tweet.write users.read offline.access"
    url = (
        "https://twitter.com/i/oauth2/authorize?"
        f"response_type=code&client_id={quote_plus(client_id)}&redirect_uri={quote_plus(redirect_uri)}"
        f"&scope={quote_plus(scope)}&state={_oauth_new_state('x')}&code_challenge={quote_plus(challenge)}&code_challenge_method=S256"
    )
    return HTMLResponse(f"<a href='{_e(url)}' target='_blank'>Открыть X авторизацию</a>")


@router.get("/accounts/callback/x")
def x_callback(code: str, state: str = ""):
    if not _oauth_check_state("x", state):
        return HTMLResponse("<span style='color:red'>Invalid OAuth state — start again from Settings → Connect X</span>", status_code=400)
    cfg = load_settings()
    token_url = "https://api.twitter.com/2/oauth2/token"
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg.get("x_redirect_uri"),
        "client_id": cfg.get("x_client_id"),
        "code_verifier": cfg.get("x_code_verifier"),
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    client_secret = (cfg.get("x_client_secret") or "").strip()
    client_id = (cfg.get("x_client_id") or "").strip()
    if client_secret and client_id:
        import base64 as _b64
        basic = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    with httpx.Client(timeout=15) as client:
        r = client.post(token_url, data=data, headers=headers)
        data = r.json()
    if "error" in data:
        return HTMLResponse(f"<span style='color:red'>X OAuth error: {_e(data)}</span>")
    from time import time as _now
    save_settings({
        "x_access_token": data.get("access_token", ""),
        "x_refresh_token": data.get("refresh_token", ""),
        "x_token_type": data.get("token_type", ""),
        "x_expires_at": int(data.get("expires_in", 0)),
        "x_expires_at_ts": int(_now()) + int(data.get("expires_in", 0)),
    })
    return HTMLResponse("<span style='color:green'>X подключён</span>")


def _x_ensure_token() -> str:
    """Kept for existing callers; the implementation lives in services/tokens.py."""
    from app.services.tokens import x_ensure_token
    return x_ensure_token()


@router.post("/x/test_tweet")
def x_test_tweet(text: str = Form("Hello from IndiePulse")):
    # Ensure valid token
    try:
        token = _x_ensure_token()
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>X token error: {_e(str(exc))}</span>")
    if not token:
        return HTMLResponse("<span style='color:orange'>X не подключён</span>")
    # Validate token scopes and status
    try:
        with httpx.Client(timeout=10) as client:
            me = client.get("https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {token}"})
            info = me.json()
            if me.status_code >= 400:
                return HTMLResponse(f"<span style='color:red'>X token invalid: {_e(info)}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>X check failed: {_e(str(exc))}</span>")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"text": text[:280]}
    with httpx.Client(timeout=15) as client:
        r = client.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
        data = r.json()
    if r.status_code >= 400:
        return HTMLResponse(f"<span style='color:red'>Ошибка X: {_e(data)}</span>")
    tid = (data.get("data") or {}).get("id")
    url = f"https://twitter.com/i/web/status/{tid}" if tid else ""
    return HTMLResponse(f"<span style='color:green'>Опубликовано: <a href='{_e(url)}' target='_blank'>ссылка</a></span>")
