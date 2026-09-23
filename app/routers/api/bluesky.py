"""Bluesky credentials test."""

from app.services.settings import load_settings
from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from loguru import logger

router = APIRouter()

@router.post("/bluesky/test")
def bluesky_test():
    """Verify Bluesky credentials by creating a session (no post is made)."""
    import html as _html
    try:
        cfg = load_settings()
        handle = (cfg.get('bluesky_handle') or '').strip()
        app_pw = (cfg.get('bluesky_app_password') or '').strip()
        if not (handle and app_pw):
            return HTMLResponse("<span style='color:#b45309'>Enter handle and app password, click Save, then Test.</span>")
        from app.services.bluesky_client import BlueskyClient
        client = BlueskyClient(handle, app_pw, (cfg.get('bluesky_service') or 'https://bsky.social'))
        client.login()
        return HTMLResponse(f"<span style='color:green'>Connected as @{_html.escape(client.handle or handle)} ✓</span>")
    except Exception as exc:
        logger.exception(f"bsky.test error: {exc}")
        return HTMLResponse(f"<span style='color:red'>Bluesky login failed: {_html.escape(str(exc))}</span>")
