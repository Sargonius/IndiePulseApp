"""TikTok OAuth connect flow, token test and the /tiktok pipeline's API."""

from typing import Optional

import httpx
from app.core.config import APP_BASE_URL as _BASE_URL
from app.db.session import get_session
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter, Body, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlmodel import Session
from urllib.parse import quote_plus
from app.routers.api.common import _e
from app.routers.api.common import _oauth_check_state
from app.routers.api.common import _oauth_new_state

router = APIRouter()

def _auth_url(cfg: dict) -> str:
    from app.services.tiktok_pipeline import auth_scopes
    client_key = (cfg.get("tiktok_client_key") or "").strip()
    redirect_uri = (cfg.get("tiktok_redirect_uri") or "").strip() or f"{_BASE_URL}/api/accounts/callback/tiktok"
    # NOTE: no video.publish — that scope needs a passed TikTok app audit, and asking
    # for a scope the app doesn't have fails the whole authorize screen (so the stats
    # scopes are only asked for when ticked on /tiktok).
    return (
        f"https://www.tiktok.com/v2/auth/authorize?"
        f"client_key={quote_plus(client_key)}&"
        f"response_type=code&"
        f"scope={quote_plus(auth_scopes(cfg))}&"
        f"redirect_uri={quote_plus(redirect_uri)}&"
        f"state={_oauth_new_state('tiktok')}"
    )


@router.get("/accounts/connect/tiktok")
def tiktok_connect_start():
    """Start TikTok OAuth flow (inbox/draft mode: video.upload, plus stats scopes on request)."""
    cfg = load_settings()
    if not (cfg.get("tiktok_client_key") or "").strip():
        return HTMLResponse("<span style='color:orange'>Save the TikTok Client Key below first</span>")
    return HTMLResponse(f"<a href='{_e(_auth_url(cfg))}' target='_blank'>Open TikTok Authorization</a>")


@router.get("/tiktok/connect_url")
def tiktok_connect_url():
    cfg = load_settings()
    if not (cfg.get("tiktok_client_key") or "").strip() or not (cfg.get("tiktok_client_secret") or "").strip():
        raise HTTPException(status_code=400, detail="Save the TikTok Client Key and Secret in ⚙️ Settings → TikTok first.")
    return {"url": _auth_url(cfg)}


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
            # what was granted (the user may untick scopes) and whose account it is
            "tiktok_scope": str(data.get("scope") or ""),
            "tiktok_open_id": str(data.get("open_id") or ""),
            "tiktok_connected_at": int(_now()),
        })
        return RedirectResponse("/tiktok?connected=1", status_code=303)
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


# ---- /tiktok pipeline (app/services/tiktok_pipeline.py) -------------------------
def _pipeline_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _item(session: Session, item_id: int):
    from app.models.models import TikTokItem
    item = session.get(TikTokItem, item_id)
    if item is None or item.archived:
        raise HTTPException(status_code=404, detail="Clip not found")
    return item


def _item_out(session: Session, item, notes: Optional[list] = None) -> dict:
    from app.services import tiktok_pipeline as tp
    return {"item": tp.item_state(session, item), "notes": notes or []}


@router.get("/tiktok/state")
def tiktok_pipeline_state(sync: int = 0, force: int = 0, session: Session = Depends(get_session)):
    """The page's data; sync=1 first catches up with TikTok (force=1: the Refresh button)."""
    from app.services import tiktok_pipeline as tp
    cfg = load_settings()
    report = {}
    if sync:
        try:
            report = tp.sync(session, cfg, force=bool(force))
        except Exception as exc:  # the page still draws what it has
            from loguru import logger
            logger.exception("tiktok pipeline sync failed")
            report = {"error": str(exc)[:300]}
        cfg = load_settings()  # a token refresh may have rotated it
    return {**tp.state(session, cfg), "sync": report}


@router.post("/tiktok/items")
def tiktok_pipeline_add(data: Optional[dict] = Body(None), session: Session = Depends(get_session)):
    """{brief_id, lang} adds a Short; {rel, project_id, lang} a video dropped on the page. Without a `lang`: the
    page's first caption language (the target region's)."""
    from app.models.models import Brief
    from app.services import tiktok_pipeline as tp
    from app.services import video_edit as ve
    data = data or {}
    lang = str(data.get("lang") or "")
    try:
        if data.get("brief_id"):
            brief = session.get(Brief, int(data["brief_id"]))
            if brief is None:
                raise HTTPException(status_code=404, detail="Short not found")
            item, notes = tp.create_from_brief(session, brief, lang)
        elif data.get("rel"):
            pid = data.get("project_id")
            item, notes = tp.create_from_upload(session, str(data["rel"]), int(pid) if pid else None, lang), []
        else:
            raise HTTPException(status_code=400, detail="Pick a Short or drop a video")
    except (tp.PipelineError, ve.EditError) as exc:
        raise _pipeline_error(exc)
    return _item_out(session, item, notes)


@router.patch("/tiktok/items/{item_id}")
def tiktok_pipeline_update(item_id: int, data: Optional[dict] = Body(None), session: Session = Depends(get_session)):
    """Caption, hashtags, checklist ticks, the target region's share, a file from ✂ Edit."""
    from app.services import tiktok_pipeline as tp
    item = _item(session, item_id)
    try:
        tp.update(session, item, data or {})
    except tp.PipelineError as exc:
        raise _pipeline_error(exc)
    return _item_out(session, item)


@router.post("/tiktok/items/{item_id}/{action}")
def tiktok_pipeline_action(item_id: int, action: str, data: Optional[dict] = Body(None),
                           session: Session = Depends(get_session)):
    """send · link {url} · lang {lang} · screen_text · drop_music · rerender · archive"""
    from app.models.models import Post
    from app.services import tiktok_pipeline as tp
    from app.services import video_edit as ve
    item = _item(session, item_id)
    data = data or {}
    post = session.get(Post, item.post_id) if item.post_id else None
    notes: list = []
    try:
        if action == "send":
            tp.send(session, item, load_settings())
        elif action == "link":
            tp.link(session, item, str(data.get("url") or ""))
            try:
                tp.sync(session, load_settings(), force=True)  # its numbers right away
            except Exception:
                pass
        elif action == "lang":
            notes = tp.set_lang(session, item, str(data.get("lang") or ""))
        elif action == "screen_text":
            tp.translate_screen_text(session, item, post)
        elif action == "drop_music":
            tp.drop_music(session, item, post)
        elif action == "rerender":
            tp.rerender(session, item)
        elif action == "archive":
            tp.archive(session, item)
            return {"ok": True}
        else:
            raise HTTPException(status_code=404, detail=f"unknown action: {action}")
    except (tp.PipelineError, ve.EditError) as exc:
        raise _pipeline_error(exc)
    session.add(item)
    session.commit()
    session.refresh(item)
    return _item_out(session, item, notes)


@router.post("/tiktok/settings")
def tiktok_pipeline_settings(data: Optional[dict] = Body(None)):
    """{want_stats}: ask for video.list + user.info.stats + user.info.profile on the next connect.
    {region}: the test's target region, a code of app/services/tiktok_regions.py ("" = none picked)."""
    from app.services import tiktok_pipeline as tp
    from app.services import tiktok_regions
    data = data or {}
    if "region" in data:
        code = str(data.get("region") or "").strip().upper()
        if code and tiktok_regions.get(code) is None:
            raise HTTPException(status_code=400, detail=f"Unknown region: {code}")
        save_settings({"tiktok_region": code})
    if "want_stats" in data:
        save_settings({"tiktok_want_stats": bool(data.get("want_stats"))})
    return tp.connection(load_settings())
