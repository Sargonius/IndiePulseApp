"""Facebook Page setup (easy setup, page assignment) and test."""

import httpx
from app.services.facebook_client import FacebookClient
from app.services.settings import load_settings
from app.services.settings import save_settings
from fastapi import APIRouter
from fastapi import Form
from fastapi.responses import HTMLResponse
from loguru import logger
from app.routers.api.common import _e

router = APIRouter()

@router.post("/facebook/test")
def facebook_test():
    try:
        cfg = load_settings()
        try:
            logger.info("fb.test start")
        except Exception:
            pass
        blocks = []
        for key_id, key_tok, label in (("fb_ru_page_id", "fb_ru_page_access_token", "RU"), ("fb_en_page_id", "fb_en_page_access_token", "EN")):
            page_id = (cfg.get(key_id) or "").strip()
            page_token = (cfg.get(key_tok) or "").strip()
            logs = []
            if page_id and page_token:
                try:
                    logs.append(f"label={label} page_id={page_id} token_len={len(page_token)}")
                    # Quick page info probe
                    import httpx as _hx
                    probe_url = f"https://graph.facebook.com/v19.0/{page_id}"
                    with _hx.Client(timeout=10) as c:
                        pr = c.get(probe_url, params={"fields": "id,name", "access_token": page_token})
                        logs.append(f"probe status={pr.status_code} body={pr.text[:300]}")
                    fb = FacebookClient(page_token)
                    msg = f"Test from IndiePulse ({label})"
                    logs.append(f"posting feed msg_len={len(msg)}")
                    resp = fb.create_post(page_id, msg)
                    pid = str(resp.get("id") or resp.get("post_id") or "")
                    logs.append(f"post resp id={pid} raw={str(resp)[:300]}")
                    link = ""
                    try:
                        if pid:
                            link = fb.get_permalink(pid)
                            logs.append(f"permalink={link}")
                    except Exception as _exc:
                        logs.append(f"permalink error: {_exc}")
                        link = ""
                    link_html = f"<a href='{_e(link)}' target='_blank'>link</a>" if link else ''
                    blocks.append(f"<div><b>{label}:</b> OK {_e(pid)} {link_html}<br/><pre>" + _e("\n".join(logs)) + "</pre></div>")
                except Exception as exc:
                    try:
                        logger.exception(f"fb.test error label={label}: {exc}")
                    except Exception:
                        pass
                    err_str = str(exc)
                    # Friendly hint for expired tokens (OAuthException code 190, subcode 463)
                    low = err_str.replace(" ", "")
                    if ('"code":190' in low) or ('"error_subcode":463' in low) or ('Error validating access token' in err_str):
                        logs.append("hint: Page Access Token expired. Generate a new Page token and update settings.")
                    logs.append(f"error: {err_str}")
                    blocks.append(f"<div><b>{_e(label)}:</b> ERROR<br/><pre>" + _e("\n".join(logs)) + "</pre></div>")
            else:
                blocks.append(f"<div><b>{_e(label)}:</b> not configured</div>")
        html = "".join(blocks)
        return HTMLResponse(f"<div>{html}</div>")
    except Exception as exc:
        try:
            logger.exception(f"fb.test outer error: {exc}")
        except Exception:
            pass
        return HTMLResponse(f"<span style='color:red'>FB test failed: {_e(str(exc))}</span>")


@router.get("/facebook/test")
def facebook_test_get():
    return facebook_test()


@router.post("/facebook/fetch_pages")
def facebook_fetch_pages(
    fb_app_id: str = Form(""),
    fb_app_secret: str = Form(""),
    fb_user_token: str = Form(""),
):
    """Easy setup: exchange a short-lived user token for a long-lived one,
    then list manageable Pages with ready-to-save Page tokens."""
    import html as _html
    cfg = load_settings()
    app_id = (fb_app_id or '').strip() or (cfg.get('fb_app_id') or '').strip()
    app_secret = (fb_app_secret or '').strip() or (cfg.get('fb_app_secret') or '').strip()
    user_token = (fb_user_token or '').strip()
    # Persist app credentials for next time
    updates = {}
    if (fb_app_id or '').strip():
        updates['fb_app_id'] = (fb_app_id or '').strip()
    if (fb_app_secret or '').strip():
        updates['fb_app_secret'] = (fb_app_secret or '').strip()
    if updates:
        save_settings(updates)
    if not user_token:
        return HTMLResponse("<span style='color:#b45309'>Paste a User Access Token from Graph API Explorer first.</span>")
    notes = []
    try:
        with httpx.Client(timeout=20) as client:
            # Step 1: exchange for a long-lived user token (60 days; Page tokens derived from it don't expire)
            if app_id and app_secret:
                r = client.get(
                    "https://graph.facebook.com/v19.0/oauth/access_token",
                    params={
                        "grant_type": "fb_exchange_token",
                        "client_id": app_id,
                        "client_secret": app_secret,
                        "fb_exchange_token": user_token,
                    },
                )
                d = r.json()
                if r.status_code < 400 and d.get('access_token'):
                    user_token = d['access_token']
                    notes.append("Token exchanged for a long-lived one ✓")
                else:
                    notes.append(f"Token exchange failed (using token as-is): {str(d)[:200]}")
            else:
                notes.append("No App ID/Secret — using token as-is (Page tokens may expire in ~1 hour)")
            # Step 2: list pages with their Page tokens
            r = client.get(
                "https://graph.facebook.com/v19.0/me/accounts",
                params={"fields": "id,name,access_token", "access_token": user_token, "limit": 50},
            )
            d = r.json()
            if r.status_code >= 400 or 'error' in d:
                raise RuntimeError(str(d.get('error') or d)[:400])
            pages = d.get('data') or []
    except Exception as exc:
        logger.exception(f"fb.fetch_pages error: {exc}")
        return HTMLResponse(f"<span style='color:red'>Failed: {_html.escape(str(exc))}</span>")
    if not pages:
        return HTMLResponse(
            "<span style='color:#b45309'>No Pages found. Make sure the token has scopes "
            "pages_show_list + pages_manage_posts and that you are an admin of the Page.</span>"
        )
    blocks = [f"<div class='muted' style='margin-bottom:6px;'>{' · '.join(_html.escape(n) for n in notes)}</div>"]
    for p in pages:
        pid = _html.escape(str(p.get('id') or ''))
        name = _html.escape(str(p.get('name') or ''))
        ptok = _html.escape(str(p.get('access_token') or ''))
        if not (pid and ptok):
            continue
        blocks.append(
            f"<div style='display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid #e5e7eb;'>"
            f"<b style='flex:1'>{name}</b><span class='muted'>{pid}</span>"
            f"<form hx-post='/api/facebook/assign_page' hx-target='#fb_status' hx-swap='innerHTML' style='display:inline'>"
            f"<input type='hidden' name='page_id' value='{pid}'/><input type='hidden' name='page_token' value='{ptok}'/>"
            f"<input type='hidden' name='page_name' value='{name}'/><input type='hidden' name='lang' value='ru'/>"
            f"<button type='submit' class='btn'>Use as RU</button></form>"
            f"<form hx-post='/api/facebook/assign_page' hx-target='#fb_status' hx-swap='innerHTML' style='display:inline'>"
            f"<input type='hidden' name='page_id' value='{pid}'/><input type='hidden' name='page_token' value='{ptok}'/>"
            f"<input type='hidden' name='page_name' value='{name}'/><input type='hidden' name='lang' value='en'/>"
            f"<button type='submit' class='btn'>Use as EN</button></form>"
            f"</div>"
        )
    return HTMLResponse("<div>" + "".join(blocks) + "</div>")


@router.post("/facebook/assign_page")
def facebook_assign_page(
    lang: str = Form(...),
    page_id: str = Form(...),
    page_token: str = Form(...),
    page_name: str = Form(""),
):
    import html as _html
    lang = (lang or '').strip().lower()
    if lang not in ('ru', 'en'):
        return HTMLResponse("<span style='color:red'>Invalid language</span>")
    # "Use as RU/EN": the Page reaches its destination even when that one was removed
    from app.services import destinations
    destinations.save_keys({
        f'fb_{lang}_page_id': (page_id or '').strip(),
        f'fb_{lang}_page_access_token': (page_token or '').strip(),
    })
    logger.info(f"fb.assign_page lang={lang} page_id={page_id} name={page_name}")
    return HTMLResponse(
        f"<span style='color:green'>Saved \"{_html.escape(page_name or page_id)}\" as {lang.upper()} page ✓</span>"
    )
