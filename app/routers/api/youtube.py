"""YouTube OAuth connect flow, the setup checklist and the "YouTube is off" alert."""

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
from app.routers.api.common import templates_api

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


def _youtube_auth_url(client_id: str) -> str:
    return (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        f"client_id={quote_plus(client_id)}&"
        f"redirect_uri={quote_plus(_YT_REDIRECT_URI)}&"
        "response_type=code&"
        f"scope={quote_plus(_YT_SCOPES)}&"
        "access_type=offline&"
        f"state={_oauth_new_state('youtube')}&"
        "prompt=consent"  # force re-issue of the refresh token
    )


@router.get("/accounts/connect/youtube")
def youtube_connect_start():
    """Return a Google consent link that yields a fresh refresh token."""
    cfg = load_settings()
    client_id = (cfg.get("yt_client_id") or "").strip()
    if not client_id:
        return HTMLResponse("<span style='color:orange'>Save the YouTube Client ID and Secret first</span>")
    return HTMLResponse(f"<a href='{_e(_youtube_auth_url(client_id))}' target='_blank' rel='noopener'>"
                        "Open Google sign-in ↗</a> — sign in to the channel’s account and allow access")


@router.get("/accounts/connect/youtube/go")
def youtube_connect_go():
    """One-click variant for the checklist: straight to Google's consent screen."""
    client_id = (load_settings().get("yt_client_id") or "").strip()
    if not client_id:
        return _page("Do step 2 first", "<p>Save the YouTube <b>Client ID</b> and <b>Client Secret</b> "
                     "in Settings, then click Connect YouTube again.</p>", 400, "error")
    return RedirectResponse(_youtube_auth_url(client_id), status_code=302)


def _page(title: str, body: str, status_code: int = 200, tone: str = "ok") -> HTMLResponse:
    """Standalone page for the consent tab: result + what to do next. Callers escape."""
    color = {"ok": "#16a34a", "warn": "#d97706", "error": "#dc2626"}[tone]
    return HTMLResponse(
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>IndiePulse · YouTube</title></head>"
        "<body style='font-family:system-ui,sans-serif;max-width:680px;margin:40px auto;padding:0 16px;"
        "line-height:1.6;color:#1e1e1e'>"
        f"<h2 style='color:{color}'>{title}</h2>{body}"
        "<p style='margin-top:24px'><a href='/accounts#yt_setup'>← YouTube settings (step-by-step setup)</a>"
        " · <a href='/analytics'>Analytics</a></p></body></html>",
        status_code=status_code)


_WARN_P = "<p style='padding:10px 12px;background:#fef3c7;border-left:4px solid #d97706;border-radius:6px'>"


def _channel_name(access_token: str) -> str:
    """'Title (@handle)' of the channel the token belongs to; '' when it can't be read."""
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get("https://www.googleapis.com/youtube/v3/channels",
                           params={"part": "snippet", "mine": "true"},
                           headers={"Authorization": f"Bearer {access_token}"})
        items = (r.json().get("items") or []) if r.status_code < 400 else []
    except Exception:
        return ""
    snippet = (items[0].get("snippet") or {}) if items else {}
    title, handle = snippet.get("title") or "", snippet.get("customUrl") or ""
    return f"{title} ({handle})" if title and handle else title


@router.get("/accounts/callback/youtube")
def youtube_callback(code: str = "", error: str = "", state: str = ""):
    """Exchange the consent code for access+refresh tokens and store them."""
    if error:
        hint = ""
        if error == "access_denied":
            hint = ("<p>If Google said <b>“Access blocked”</b> or <b>“has not completed the Google "
                    "verification process”</b>, the app is still in <b>Testing</b>: do step 3 "
                    "(Publish app) and connect again. If you clicked Cancel yourself, just start over.</p>")
        return _page("Google denied access", f"<p>Google OAuth error: <code>{_e(error)}</code></p>{hint}",
                     400, "error")
    if not _oauth_check_state("youtube", state):
        return _page("Link expired", "<p>This sign-in link was already used or has expired — "
                     "go back to the YouTube settings and click <b>Connect YouTube</b> again.</p>", 400, "error")
    cfg = load_settings()
    client_id = (cfg.get("yt_client_id") or "").strip()
    client_secret = (cfg.get("yt_client_secret") or "").strip()
    if not client_id or not client_secret:
        return _page("No Client ID / Secret", "<p>Save the <b>Client ID</b> and <b>Client Secret</b> "
                     "(step 2) and connect again.</p>", 400, "error")
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
            return _page("Google didn’t issue a token", f"<p><code>{_e(data)}</code></p>"
                         "<p>Click <b>Connect YouTube</b> again — the authorization code works only once.</p>", 400, "error")
        from time import time as _now
        now = int(_now())
        # Documented for time-limited grants; apps in Testing get 7-day tokens —
        # remember the expiry so the checklist and the alert can warn in time.
        refresh_ttl = int(data.get("refresh_token_expires_in") or 0)
        channel = _channel_name(access_token)
        updates = {"yt_access_token": access_token,
                   "yt_expires_at": now + int(data.get("expires_in") or 3600),
                   "yt_connected_at": now,
                   "yt_auth_error": "", "yt_auth_error_at": 0,
                   "yt_refresh_expires_at": now + refresh_ttl if refresh_ttl else 0,
                   "yt_channel": channel}
        if refresh_token:
            updates["yt_refresh_token"] = refresh_token
        save_settings(updates)
        try:
            # Stats of every video failed while the token was dead — refetch them all now
            from app.jobs.tasks import backfill_youtube_metrics
            backfill_youtube_metrics.schedule(delay=5)
        except Exception as exc:
            logger.warning(f"yt metrics backfill not scheduled: {exc}")
        body = (f"<p>Channel: <b>{_e(channel)}</b></p>" if channel else
                "<p>Couldn’t detect the channel — check step 1 (is YouTube Data API v3 enabled?).</p>")
        body += ("<p>Stats for all videos will update within a minute. You can close this tab — "
                 "in IndiePulse, click <b>↻ Check again</b> in the YouTube settings.</p>")
        tone = "ok"
        if refresh_ttl:
            from app.services.youtube_setup import fmt_when
            body += (f"<p>The token is valid until <b>{_e(fmt_when(now + refresh_ttl))}</b>: the app is in "
                     "<b>Testing</b>, so Google issues 7-day tokens. IndiePulse will remind you 2 days before "
                     "it ends (banner and tray notification) — reconnecting takes one click.</p>")
        if not refresh_token:
            tone = "warn"
            body += (f"{_WARN_P}⚠ Google didn’t return a refresh token — access will be lost in an hour. Open "
                     "<a href='https://myaccount.google.com/permissions' target='_blank' rel='noopener'>"
                     "myaccount.google.com/permissions</a>, remove this app’s access and click "
                     "Connect YouTube again.</p>")
        return _page("✅ YouTube connected", body, 200, tone)
    except Exception as exc:
        return _page("Connection failed", f"<p><code>{_e(exc)}</code></p>"
                     "<p>Click <b>Connect YouTube</b> again.</p>", 500, "error")


@router.get("/youtube/setup", response_class=HTMLResponse)
def youtube_setup(request: Request):
    """Accounts → YouTube checklist. Checks the token live: refreshing it records or
    clears yt_auth_error, so the page never shows a stale verdict."""
    from sqlmodel import Session
    from app.core.redact import redact
    from app.db.session import engine
    from app.services import youtube_setup as ys
    from app.services.tokens import yt_access_token
    cfg = load_settings()
    live_note = ""
    if all((cfg.get(k) or "").strip() for k in ("yt_refresh_token", "yt_client_id", "yt_client_secret")):
        try:
            yt_access_token()
        except Exception as exc:
            if not getattr(exc, "reauth", False):  # network trouble is not a verdict
                live_note = f"Couldn’t check the token right now: {redact(str(exc))[:200]}"
        cfg = load_settings()
    with Session(engine) as session:
        metrics = ys.metrics_summary(session)
    state = ys.setup_state(cfg, metrics)
    return templates_api.TemplateResponse("partials/youtube_setup.html", {
        "request": request,
        "s": state,
        "links": ys.console_links(cfg.get("yt_client_id") or ""),
        "redirect_uri": _YT_REDIRECT_URI,
        "channel": (cfg.get("yt_channel") or "").strip(),
        "connected": bool((cfg.get("yt_refresh_token") or "").strip()),
        "metrics_days": cfg.get("yt_metrics_days"),
        "live_note": live_note,
        "quota_hit": "quotaExceeded" in state["last_metric_error"],
    })


@router.get("/youtube/alert", response_class=HTMLResponse)
def youtube_alert():
    """Banner on every page while the YouTube token is about to expire or has stopped
    working, with a one-click reconnect (settings only, no network)."""
    from app.services.youtube_setup import alert
    a = alert(load_settings())
    if not a:
        return HTMLResponse("")
    bg, border, fg = {"error": ("#fee2e2", "#dc2626", "#991b1b"),
                      "warn": ("#fef3c7", "#d97706", "#92400e")}[a["level"]]
    buttons = ("<a href='/api/accounts/connect/youtube/go' target='_blank' rel='noopener' "
               "class='btn btn-primary' style='text-decoration:none'>Reconnect ↗</a>") if a["reconnect"] else ""
    buttons += "<a href='/accounts#yt_setup' class='btn' style='text-decoration:none'>Step by step →</a>"
    return HTMLResponse(
        f"<div id='yt_alert' role='alert' style='margin:0 0 16px;padding:10px 14px;background:{bg};"
        f"border-left:4px solid {border};border-radius:6px;color:{fg};display:flex;gap:12px;"
        f"align-items:center;flex-wrap:wrap'><span>⚠ {_e(a['text'])}</span>"
        f"<span style='margin-left:auto;display:flex;gap:8px'>{buttons}</span></div>")
