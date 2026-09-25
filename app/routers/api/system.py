"""App-level endpoints: restart/autoreload, settings save + status, API health check."""

from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi import Query
from fastapi.responses import HTMLResponse
from fastapi.responses import Response
from loguru import logger
from typing import Optional
from app.routers.api.common import _e

router = APIRouter()

def _render_autoreload_button_html(enabled: bool) -> str:
    cls = "icon-btn" + (" active" if enabled else "")
    title = "Автообновление включено" if enabled else "Включить автообновление"
    # Render two controls inside the fixed wrapper span (#nav_ar_btn):
    # 1) Autoreload toggle button
    # 2) Restart button that triggers full app restart
    return (
        f"<div class=\"row\" style=\"gap:6px;flex-wrap:nowrap;\">"
        f"<button class=\"{cls}\" title=\"{title}\" "
        f"hx-post=\"/api/system/toggle_autoreload\" hx-target=\"#nav_ar_btn\" hx-swap=\"innerHTML\">↻</button>"
        f"<button class=\"icon-btn\" title=\"Перезапустить приложение\" "
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
            "<span style='color:red'>Перезапуск работает, когда IndiePulse запущен через IndiePulse.pyw (значок в трее)</span>",
            status_code=409)
    try:
        RESTART_REQUEST_FILE.touch()
    except OSError as exc:
        return HTMLResponse(f"<span style='color:red'>Restart failed: {_e(str(exc))}</span>", status_code=500)
    return HTMLResponse("<span style='color:orange'>Перезапуск…</span>")


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


@router.post("/settings/save")
def save_settings_endpoint(
	vk_client_id: str = Form(""),
	vk_client_secret: str = Form(""),
	vk_group_id: str = Form(""),
	vk_redirect_uri: str = Form(""),
	vk_access_token: str = Form(""),
	vk_user_access_token: str = Form(""),
	x_client_id: str = Form(""),
	x_client_secret: str = Form(""),
	x_redirect_uri: str = Form(""),
	x_api_key: str = Form(""),
	x_api_secret: str = Form(""),
	x_oauth_token: str = Form(""),
	x_oauth_token_secret: str = Form(""),
	yt_client_id: str = Form(""),
	yt_client_secret: str = Form(""),
	yt_access_token: str = Form(""),
	yt_refresh_token: str = Form(""),
	tiktok_client_key: str = Form(""),
	tiktok_client_secret: str = Form(""),
	tiktok_redirect_uri: str = Form(""),
	fb_app_id: str = Form(""),
	fb_app_secret: str = Form(""),
	fb_ru_page_id: str = Form(""),
	fb_en_page_id: str = Form(""),
	fb_ru_page_access_token: str = Form(""),
	fb_en_page_access_token: str = Form(""),
	ig_user_id: str = Form(""),
	ig_access_token: str = Form(""),
	bluesky_handle: str = Form(""),
	bluesky_app_password: str = Form(""),
	bluesky_service: str = Form(""),
	reddit_client_id: str = Form(""),
	reddit_client_secret: str = Form(""),
	reddit_username: str = Form(""),
	reddit_password: str = Form(""),
	reddit_subreddit_en: str = Form(""),
	# OpenAI
	openai_api_key: str = Form(""),
	openai_model: str = Form(""),
	# Telegram
	tg_bot_token: str = Form(""),
	tg_chat_id_ru: str = Form(""),
	# Discord (bot token for reading message reactions into analytics)
	discord_bot_token: str = Form(""),
	# UI
	ui_language: str = Form(""),
):
	# Only update keys that are non-empty to avoid wiping other settings
	updates = {}
	if vk_client_id.strip():
		updates["vk_client_id"] = vk_client_id.strip()
	if vk_client_secret.strip():
		updates["vk_client_secret"] = vk_client_secret.strip()
	if vk_group_id.strip():
		updates["vk_group_id"] = vk_group_id.strip()
	if vk_redirect_uri.strip():
		updates["vk_redirect_uri"] = vk_redirect_uri.strip()
	if vk_access_token.strip():
		updates["vk_access_token"] = vk_access_token.strip()
	if vk_user_access_token.strip():
		updates["vk_user_access_token"] = vk_user_access_token.strip()
	if x_client_id.strip():
		updates["x_client_id"] = x_client_id.strip()
	if x_client_secret.strip():
		updates["x_client_secret"] = x_client_secret.strip()
	if x_redirect_uri.strip():
		updates["x_redirect_uri"] = x_redirect_uri.strip()
	if x_api_key.strip():
		updates["x_api_key"] = x_api_key.strip()
	if x_api_secret.strip():
		updates["x_api_secret"] = x_api_secret.strip()
	if x_oauth_token.strip():
		updates["x_oauth_token"] = x_oauth_token.strip()
	if x_oauth_token_secret.strip():
		updates["x_oauth_token_secret"] = x_oauth_token_secret.strip()
	if yt_client_id.strip():
		updates["yt_client_id"] = yt_client_id.strip()
	if yt_client_secret.strip():
		updates["yt_client_secret"] = yt_client_secret.strip()
	if yt_access_token.strip():
		updates["yt_access_token"] = yt_access_token.strip()
	if yt_refresh_token.strip():
		updates["yt_refresh_token"] = yt_refresh_token.strip()
	if tiktok_client_key.strip():
		updates["tiktok_client_key"] = tiktok_client_key.strip()
	if tiktok_client_secret.strip():
		updates["tiktok_client_secret"] = tiktok_client_secret.strip()
	if tiktok_redirect_uri.strip():
		updates["tiktok_redirect_uri"] = tiktok_redirect_uri.strip()
	if fb_app_id.strip():
		updates["fb_app_id"] = fb_app_id.strip()
	if fb_app_secret.strip():
		updates["fb_app_secret"] = fb_app_secret.strip()
	if bluesky_handle.strip():
		updates["bluesky_handle"] = bluesky_handle.strip().lstrip('@')
	if bluesky_app_password.strip():
		updates["bluesky_app_password"] = bluesky_app_password.strip()
	if bluesky_service.strip():
		updates["bluesky_service"] = bluesky_service.strip()
	if fb_ru_page_id.strip():
		updates["fb_ru_page_id"] = fb_ru_page_id.strip()
	if fb_en_page_id.strip():
		updates["fb_en_page_id"] = fb_en_page_id.strip()
	if fb_ru_page_access_token.strip():
		updates["fb_ru_page_access_token"] = fb_ru_page_access_token.strip()
	if fb_en_page_access_token.strip():
		updates["fb_en_page_access_token"] = fb_en_page_access_token.strip()
	if ig_user_id.strip():
		updates["ig_user_id"] = ig_user_id.strip()
	if ig_access_token.strip():
		updates["ig_access_token"] = ig_access_token.strip()
	if reddit_client_id.strip():
		updates["reddit_client_id"] = reddit_client_id.strip()
	if reddit_client_secret.strip():
		updates["reddit_client_secret"] = reddit_client_secret.strip()
	if reddit_username.strip():
		updates["reddit_username"] = reddit_username.strip()
	if reddit_password.strip():
		updates["reddit_password"] = reddit_password.strip()
	if reddit_subreddit_en.strip():
		updates["reddit_subreddit_en"] = reddit_subreddit_en.strip()
	if openai_api_key.strip():
		updates["openai_api_key"] = openai_api_key.strip()
	if openai_model.strip():
		updates["openai_model"] = openai_model.strip()
	if tg_bot_token.strip():
		updates["tg_bot_token"] = tg_bot_token.strip()
	if tg_chat_id_ru.strip():
		updates["tg_chat_id_ru"] = tg_chat_id_ru.strip()
	if discord_bot_token.strip():
		updates["discord_bot_token"] = discord_bot_token.strip()
	# UI language
	if ui_language.strip():
		lang = ui_language.strip().lower()
		if lang in ("en", "ru"):
			updates["ui_language"] = lang
	save_settings(updates)
	# Ask htmx to reload the page so new values are visible
	return Response(content="", headers={"HX-Redirect": "/accounts"})


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
        key = (cfg.get('openai_api_key') or '').strip()
        if not key:
            return {'status': 'not_configured', 'message': 'No API key'}
        return {'status': 'configured', 'message': 'API key set'}

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
