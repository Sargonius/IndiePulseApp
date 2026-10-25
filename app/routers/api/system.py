"""App-level endpoints: restart/autoreload, IndiePulse's window (its mark, its links to other sites), settings save
(per section, from its schema) + status, API health check."""

from app.services.settings import load_settings
from app.core import settings_schema
from app.services import destinations
from fastapi import APIRouter
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.responses import RedirectResponse
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel
from typing import Optional
from urllib.parse import urlsplit
from app.routers.api.common import _e
from app.routers.api.common import settings_back_url
from app.routers.api.common import templates_api

router = APIRouter()

def _render_autoreload_button_html(enabled: bool) -> str:
    cls = "icon-btn" + (" active" if enabled else "")
    title = "Auto-reload is on" if enabled else "Turn on auto-reload"
    # Render two controls inside the fixed wrapper span (#nav_ar_btn):
    # 1) Autoreload toggle button
    # 2) Restart button that triggers full app restart
    return (
        f"<div class=\"row\" style=\"gap:6px;flex-wrap:nowrap;\">"
        f"<button class=\"{cls}\" title=\"{title}\" "
        f"hx-post=\"/api/system/toggle_autoreload\" hx-target=\"#nav_ar_btn\" hx-swap=\"innerHTML\">↻</button>"
        f"<button class=\"icon-btn\" title=\"Restart the app\" "
        f"hx-post=\"/api/system/restart\" hx-target=\"#nav_ar_btn\" hx-swap=\"innerHTML\">⟲</button>"
        f"</div>"
    )


@router.post("/system/restart")
def system_restart():
    """Ask the tray supervisor to restart the server and the worker. The page's
    connection monitor (base.html) reloads it once the new server answers."""
    from app.desktop import RESTART_REQUEST_FILE, is_supervised
    if not is_supervised():
        return HTMLResponse(
            "<span style='color:red'>Restart works only when IndiePulse runs from IndiePulse.exe (tray icon)</span>",
            status_code=409)
    try:
        RESTART_REQUEST_FILE.touch()
    except OSError as exc:
        return HTMLResponse(f"<span style='color:red'>Restart failed: {_e(str(exc))}</span>", status_code=500)
    return HTMLResponse("<span style='color:orange'>Restarting…</span>")


@router.post("/system/toggle_autoreload")
def system_toggle_autoreload(enable: Optional[bool] = Form(None)):
    import os
    try:
        flag = os.path.abspath('.autoreload')
        if enable is None:
            # toggle
            enable = not os.path.exists(flag)
        if enable:
            with open(flag, 'w') as f:
                f.write('1')
            return HTMLResponse(_render_autoreload_button_html(True))
        else:
            if os.path.exists(flag):
                os.remove(flag)
            return HTMLResponse(_render_autoreload_button_html(False))
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Toggle failed: {_e(str(exc))}</span>", status_code=500)


@router.get("/system/autoreload_status")
def system_autoreload_status():
    import os
    enabled = os.path.exists('.autoreload')
    return HTMLResponse(_render_autoreload_button_html(enabled))


# ── IndiePulse's own window (app/desktop/window.py) ─────────────────────────
# The cookie that marks it. Only the window's own profile ever has it: a tab of the user's browser pointed at
# 127.0.0.1 doesn't, and nothing changes there. base.html reads it.
APP_WINDOW_COOKIE = "ip_window"
_APP_WINDOW_DAYS = 400  # the longest Chrome keeps a cookie; every start of the window sets it again
_EXTERNAL_SCHEMES = ("http", "https", "mailto")
_EXTERNAL_MAX = 16384  # a Gmail compose link with a whole letter in it (outreach.js) fits


@router.get("/system/app_window")
def system_app_window(next_page: str = Query("/", alias="next")):
    """Where the window opens (window.start_url): marks it as IndiePulse's window, then goes on to `next`, a page of
    this app. Its links to other sites then open in the default browser (base.html → open_external below)."""
    local = (next_page.startswith("/") and next_page[1:2] not in ("/", "\\")
             and all(" " < c < "\x7f" for c in next_page))
    resp = RedirectResponse(next_page if local else "/", status_code=303)
    resp.set_cookie(APP_WINDOW_COOKIE, "app", max_age=_APP_WINDOW_DAYS * 86400, path="/", samesite="lax",
                    httponly=True)
    return resp


def external_site(url: str) -> str:
    """The site of a link open_external opens: its host, or "mailto". ValueError: not such a link — http(s) with a
    host, or mailto; printable ASCII without spaces or quotes (what a browser makes of a link: it percent-encodes the
    rest), not too long."""
    if not url or len(url) > _EXTERNAL_MAX:
        raise ValueError("no link, or a link too long")
    if any(not (" " < c < "\x7f") or c == '"' for c in url):
        raise ValueError("control characters, spaces or quotes in the link")
    parts = urlsplit(url)  # ValueError too: a broken [IPv6] host
    scheme = parts.scheme.lower()
    if scheme not in _EXTERNAL_SCHEMES:
        raise ValueError("only http, https and mailto links open in the browser")
    if scheme == "mailto":
        return "mailto"
    if not parts.hostname:
        raise ValueError("a web link without a site")
    return parts.hostname


class ExternalLink(BaseModel):
    url: str


@router.post("/system/open_external")
def system_open_external(link: ExternalLink):
    """A link of IndiePulse's window to another site (Discord, PayPal, YouTube Studio…): opened in the default browser
    (or mail app), where the user is signed in, instead of in the window's profile of its own. Cross-site POSTs never
    get here (main.py, block_cross_site_writes). Only the site goes to the log."""
    from app.desktop import window
    try:
        site = external_site(link.url)
    except ValueError as exc:
        logger.warning(f"open_external refused a link: {exc}")
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        window.open_external(link.url)
    except OSError as exc:
        why = exc.strerror or str(exc)  # os.startfile's has the link in str(): not for the log
        logger.warning(f"open_external couldn't open a link to {site}: {why}")
        raise HTTPException(status_code=500, detail=f"Windows couldn't open it: {why}")
    logger.info(f"link to {site} opened in the default browser")
    return {"ok": True}


@router.post("/settings/save")
async def save_settings_endpoint(request: Request):
	"""The hand-written Settings forms (VK, X, YouTube stats, Facebook's manual fields, TikTok): any form field
	of app/core/settings_schema.py, stored only when it isn't empty — so a field left blank never wipes a value
	(a secret isn't shown back) — and when it reads (a bad number is skipped). The other sections save through
	/settings/section/<id> below."""
	updates = settings_schema.parse_legacy(await request.form())
	# a chat id / Page / subreddit set up here reaches a destination (Settings → Languages & destinations)
	destinations.save_keys(updates)
	# Ask htmx to reload the page so new values are visible
	return Response(content="", headers={"HX-Redirect": settings_back_url(request)})


@router.post("/settings/section/{section_id}", response_class=HTMLResponse)
async def save_settings_section(request: Request, section_id: str):
	"""One section's form made from its schema (partials/settings_form.html): every field it shows is checked;
	anything wrong → the form again with what to fix (422) and nothing saved; else only its keys are written. A
	secret left empty keeps the saved one; `_group` = one of a hand-written section's forms of fields."""
	s = settings_schema.section(section_id)
	if s is None or not s.form_fields:
		return HTMLResponse("unknown settings section", status_code=404)
	form = await request.form()
	group = str(form.get("_group") or "")
	keys = settings_schema.group_keys(s, group)
	if (group or s.custom) and keys is None:
		# a hand-written section saves its own fields; only its groups made from the schema come here
		return HTMLResponse("unknown settings group", status_code=404)
	updates, errors = settings_schema.parse_form(s, form, keys)
	if errors:
		view = settings_schema.form_view(s, load_settings(), keys, group, submitted=form, errors=errors,
										 note="Not saved: " + "; ".join(errors.values()), note_error=True)
		return templates_api.TemplateResponse("partials/settings_form.html", {"request": request, "form": view},
											  status_code=422)
	if updates:
		# the Telegram chat id, the Discord webhook, the subreddit… reach a destination even when the migration
		# made none for them (app.services.destinations.adopt_keys)
		destinations.save_keys(updates)
	logger.info(f"settings section {section_id} saved: {sorted(updates)}")
	view = settings_schema.form_view(s, load_settings(), keys, group, note="Saved ✓")
	return templates_api.TemplateResponse("partials/settings_form.html", {"request": request, "form": view})


@router.post("/health/run")
def health_run(only: str = Query("")):
    """Live-check every configured integration (or just `only`). See services/health_check.py."""
    from app.services.health_check import run_checks
    return run_checks(only.strip() or None)


@router.get("/health/last")
def health_last():
    from app.services.health_check import load_last, publish_history
    last = load_last() or {"ran_at": None, "results": []}
    try:
        last["history"] = publish_history()  # always fresh — it's a cheap local DB read
    except Exception as exc:
        logger.warning(f"health.history failed: {exc}")
    return last


@router.get("/settings/status")
def settings_status():
    """Check the status of all platform integrations."""
    cfg = load_settings()
    
    def check_discord():
        url = (cfg.get('discord_webhook_ru') or cfg.get('discord_webhook_en') or cfg.get('discord_webhook_url') or '').strip()
        if not url:
            return {'status': 'not_configured', 'message': 'No webhook URL'}
        return {'status': 'configured', 'message': 'Webhook URL set'}
    
    def check_telegram():
        token = (cfg.get('tg_bot_token') or '').strip()
        chat_id = (cfg.get('tg_chat_id_ru') or '').strip()
        if not token:
            return {'status': 'not_configured', 'message': 'No bot token'}
        if not chat_id:
            return {'status': 'partial', 'message': 'Missing chat ID'}
        return {'status': 'configured', 'message': 'Bot token and chat ID set'}
    
    def check_vk():
        token = (cfg.get('vk_access_token') or '').strip()
        group_id = (cfg.get('vk_group_id') or '').strip()
        if not token:
            return {'status': 'not_configured', 'message': 'No access token'}
        if not group_id:
            return {'status': 'partial', 'message': 'Missing group ID'}
        return {'status': 'configured', 'message': 'Token and group ID set'}
    
    def check_x():
        token = (cfg.get('x_access_token') or '').strip()
        if not token:
            return {'status': 'not_configured', 'message': 'No access token'}
        if cfg.get('x_credits_error_at'):
            return {'status': 'not_configured', 'message': '💳 Out of X API credits — see on the right'}
        api_key = (cfg.get('x_api_key') or '').strip()
        oauth_token = (cfg.get('x_oauth_token') or '').strip()
        if not (api_key and oauth_token):
            return {'status': 'partial', 'message': 'Text only (no media upload)'}
        return {'status': 'configured', 'message': 'Full access configured'}
    
    def check_youtube():
        access = (cfg.get('yt_access_token') or '').strip()
        refresh = (cfg.get('yt_refresh_token') or '').strip()
        client_id = (cfg.get('yt_client_id') or '').strip()
        if not (access and refresh):
            return {'status': 'not_configured', 'message': 'No tokens'}
        if not client_id:
            return {'status': 'partial', 'message': 'Missing client ID'}
        from app.services.youtube_setup import connection, fmt_when
        conn = connection(cfg)
        if conn['state'] == 'error':
            return {'status': 'not_configured', 'message': 'Reconnect needed — see the steps on the right'}
        if conn['state'] == 'expired':
            return {'status': 'not_configured', 'message': 'Token expired — reconnect'}
        if conn['state'] == 'expiring':
            return {'status': 'partial', 'message': f"Token valid until {fmt_when(conn['expires_at'])} — reconnect"}
        if conn['expires_at']:
            return {'status': 'configured', 'message': f"Token valid until {fmt_when(conn['expires_at'])}"}
        return {'status': 'configured', 'message': 'OAuth tokens set'}
    
    def check_facebook():
        ru_id = (cfg.get('fb_ru_page_id') or '').strip()
        ru_token = (cfg.get('fb_ru_page_access_token') or '').strip()
        en_id = (cfg.get('fb_en_page_id') or '').strip()
        en_token = (cfg.get('fb_en_page_access_token') or '').strip()
        ru_ok = bool(ru_id and ru_token)
        en_ok = bool(en_id and en_token)
        if not (ru_ok or en_ok):
            return {'status': 'not_configured', 'message': 'No pages configured'}
        if ru_ok and en_ok:
            return {'status': 'configured', 'message': 'RU and EN pages set'}
        return {'status': 'partial', 'message': 'Only one page configured'}
    
    def check_reddit():
        cid = (cfg.get('reddit_client_id') or '').strip()
        csec = (cfg.get('reddit_client_secret') or '').strip()
        uname = (cfg.get('reddit_username') or '').strip()
        pwd = (cfg.get('reddit_password') or '').strip()
        sub = (cfg.get('reddit_subreddit_en') or '').strip()
        if not (cid and csec and uname and pwd):
            return {'status': 'not_configured', 'message': 'Missing credentials'}
        if not sub:
            return {'status': 'partial', 'message': 'Missing subreddit'}
        return {'status': 'configured', 'message': 'Full credentials set'}
    
    def check_tiktok():
        token = (cfg.get('tiktok_access_token') or '').strip()
        if not token:
            return {'status': 'not_configured', 'message': 'No access token (export mode)'}
        return {'status': 'configured', 'message': 'API access configured'}
    
    def check_openai():
        # the "AI & translation" section: which AI the translate buttons use, and DeepL
        from app.services import llm
        which = llm.provider(cfg)
        if which:
            model = cfg.get(f'{which}_model') or ''
            extra = ' + DeepL' if (cfg.get('deepl_api_key') or '').strip() else ''
            return {'status': 'configured', 'message': f'{llm.PROVIDERS[which]} {model}{extra}'.strip()}
        if (cfg.get('openai_api_key') or cfg.get('anthropic_api_key') or '').strip():
            return {'status': 'partial', 'message': 'The AI picked has no key'}
        return {'status': 'not_configured', 'message': 'No AI key: free translators only'}

    def check_bluesky():
        handle = (cfg.get('bluesky_handle') or '').strip()
        app_pw = (cfg.get('bluesky_app_password') or '').strip()
        if not (handle or app_pw):
            return {'status': 'not_configured', 'message': 'No handle / app password'}
        if not (handle and app_pw):
            return {'status': 'partial', 'message': 'Missing handle or app password'}
        return {'status': 'configured', 'message': f'@{handle}'}

    return {
        'discord': check_discord(),
        'telegram': check_telegram(),
        'vk': check_vk(),
        'x': check_x(),
        'youtube': check_youtube(),
        'facebook': check_facebook(),
        'reddit': check_reddit(),
        'tiktok': check_tiktok(),
        'bluesky': check_bluesky(),
        'openai': check_openai(),
    }
