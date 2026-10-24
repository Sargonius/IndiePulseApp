"""X (Twitter) OAuth2 PKCE connect flow, test tweet, and the API-credits banner/guide."""

import base64
import hashlib
import httpx
import secrets
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi import Request
from fastapi.responses import HTMLResponse
from urllib.parse import quote_plus
from app.routers.api.common import _e
from app.routers.api.common import _oauth_check_state
from app.routers.api.common import _oauth_new_state
from app.routers.api.common import templates_api
from app.services import x_billing

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
        return HTMLResponse("<span style='color:orange'>Enter the X Client ID and Redirect URI</span>")
    verifier, challenge = _pkce_pair()
    save_settings({"x_code_verifier": verifier})
    scope = "tweet.read tweet.write users.read offline.access"
    url = (
        "https://twitter.com/i/oauth2/authorize?"
        f"response_type=code&client_id={quote_plus(client_id)}&redirect_uri={quote_plus(redirect_uri)}"
        f"&scope={quote_plus(scope)}&state={_oauth_new_state('x')}&code_challenge={quote_plus(challenge)}&code_challenge_method=S256"
    )
    return HTMLResponse(f"<a href='{_e(url)}' target='_blank'>Open X authorization</a>")


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
    return HTMLResponse("<span style='color:green'>X connected</span>")


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
        return HTMLResponse("<span style='color:orange'>X isn’t connected</span>")
    # Validate token scopes and status
    try:
        with httpx.Client(timeout=10) as client:
            me = client.get("https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {token}"})
            info = me.json()
            if x_billing.remember(me.status_code, info, billed=False):
                return HTMLResponse(f"<span style='color:red'>Out of X API credits (402) — the tweet wasn’t sent. "
                                    f"Top up your balance at <a href='{x_billing.CONSOLE_URL}' target='_blank' "
                                    "rel='noopener'>console.x.com</a>.</span>")
            if me.status_code >= 400:
                return HTMLResponse(f"<span style='color:red'>X token invalid: {_e(info)}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>X check failed: {_e(str(exc))}</span>")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"text": text[:280]}
    with httpx.Client(timeout=15) as client:
        r = client.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
        data = r.json()
    if x_billing.remember(r.status_code, data):
        return HTMLResponse(f"<span style='color:red'>Out of X API credits (402) — top up your balance at "
                            f"<a href='{x_billing.CONSOLE_URL}' target='_blank' rel='noopener'>console.x.com</a>. "
                            "Details: “X API billing” on the right.</span>")
    if r.status_code >= 400:
        return HTMLResponse(f"<span style='color:red'>X error: {_e(data)}</span>")
    tid = (data.get("data") or {}).get("id")
    url = f"https://twitter.com/i/web/status/{tid}" if tid else ""
    return HTMLResponse(f"<span style='color:green'>Published: <a href='{_e(url)}' target='_blank'>link</a></span>")


def _failed_posts() -> int:
    from sqlmodel import Session
    from app.db.session import engine
    with Session(engine) as session:
        return x_billing.failed_posts(session)


def _banner(note: str = "") -> str:
    a = x_billing.alert(load_settings())
    if not a:
        return ""
    failed = _failed_posts()
    text = f"💳 {_e(a['text'])}"
    if failed:
        text += f" Posts not sent: <b>{failed}</b>."
    if note:
        text += f" <i>{_e(note)}</i>"
    buttons = (f"<a href='{x_billing.CONSOLE_URL}' target='_blank' rel='noopener' class='btn btn-primary' "
               "style='text-decoration:none'>Top up ↗</a>"
               "<button type='button' class='btn' hx-post='/api/x/credits_check' hx-target='#x_alert' "
               "hx-swap='outerHTML'>I topped up — check</button>"
               "<a href='/accounts#x_billing' class='btn' style='text-decoration:none'>What spends credits →</a>")
    return ("<div id='x_alert' role='alert' style='margin:0 0 16px;padding:10px 14px;background:#fee2e2;"
            "border-left:4px solid #dc2626;border-radius:6px;color:#991b1b;display:flex;gap:12px;"
            f"align-items:center;flex-wrap:wrap'><span>{text}</span>"
            f"<span style='margin-left:auto;display:flex;gap:8px;flex-wrap:wrap'>{buttons}</span></div>")


@router.get("/x/alert", response_class=HTMLResponse)
def x_alert():
    """Banner on every page while X answers 402 'credits depleted' (settings + DB, no network)."""
    return HTMLResponse(_banner())


@router.get("/x/billing", response_class=HTMLResponse)
def x_billing_panel(request: Request, note: str = ""):
    """Accounts → X: what the API costs, what IndiePulse spent lately, the balance state."""
    from sqlmodel import Session
    from app.db.session import engine
    from app.services.youtube_setup import fmt_when
    cfg = load_settings()
    since = x_billing.depleted_since(cfg)
    with Session(engine) as session:
        failed = x_billing.failed_posts(session) if since else 0
        usage = x_billing.usage(session)
    return templates_api.TemplateResponse("partials/x_billing.html", {
        "request": request,
        "connected": bool((cfg.get("x_access_token") or cfg.get("x_refresh_token") or "").strip()),
        "since": fmt_when(since) if since else "",
        "failed": failed,
        "u": usage,
        "b": x_billing,
        "note": note,
    })


@router.post("/x/credits_check", response_class=HTMLResponse)
def x_credits_check(request: Request, view: str = "banner"):
    """'I topped up' button, in the banner and in Accounts → X."""
    depleted, note = x_billing.probe()
    if view == "guide":
        if depleted is False:
            note = "✓ X accepts requests — you have credits."
        return x_billing_panel(request, note)
    if depleted is False:
        failed = _failed_posts()
        retry = (f" While the balance was empty, <b>{failed}</b> {'post' if failed == 1 else 'posts'} "
                 "didn’t go out — <a href='/jobs'>Jobs</a> → Retry." if failed else "")
        return HTMLResponse(
            "<div id='x_alert' role='status' style='margin:0 0 16px;padding:10px 14px;background:#dcfce7;"
            "border-left:4px solid #16a34a;border-radius:6px;color:#166534'>"
            f"✓ X API accepts requests again — you have credits.{retry}</div>")
    return HTMLResponse(_banner(note))
