from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from pydantic import BaseModel
from typing import List, Optional
from sqlmodel import select
from app.db.session import get_session
from sqlmodel import Session
from app.models.models import Brief, Post
from app.services.discord_client import DiscordClient
from app.services.settings import load_settings, save_settings
import httpx
from urllib.parse import quote_plus
from app.services.draft_store import save_draft
from fastapi import UploadFile, File
from typing import Optional, List
from loguru import logger
import os, time
import os
import base64

router = APIRouter(prefix="/api")

class TranslateReq(BaseModel):
    ru_text: str

class TranslateResp(BaseModel):
    en_text: str

class HashtagReq(BaseModel):
    text: str
    platform: Optional[str] = None

class HashtagResp(BaseModel):
    tags: List[str]

class BriefIn(BaseModel):
    type: str
    text_ru: Optional[str] = None
    text_en: Optional[str] = None
    tags_json: Optional[str] = None

class BriefOut(BaseModel):
    id: int

@router.post("/translate")
def translate(ru_text: Optional[str] = Form(None), text_ru: Optional[str] = Form(None)):
    src = (ru_text or text_ru or "").strip()
    if not src:
        return HTMLResponse("<textarea id=\"text_en\" name=\"text_en\" rows=\"6\" cols=\"80\"></textarea>")
    def contains_cyrillic(s: str) -> bool:
        return any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in s)
    en = ""
    err = None
    try:
        from deep_translator import GoogleTranslator, MyMemoryTranslator, LibreTranslator
        # Prefer public LibreTranslate instances first
        for host in (
            'https://libretranslate.de',
            'https://translate.astian.org',
        ):
            try:
                en = LibreTranslator(source='ru', target='en', host=host).translate(src) or ""
                if en and not contains_cyrillic(en):
                    break
            except Exception as e_lt:
                err = e_lt
                # embed provider info in the textarea as a comment for debugging in UI
                pass
        # Try Google then MyMemory as additional fallbacks
        if not en or contains_cyrillic(en):
            try:
                en = GoogleTranslator(source='ru', target='en').translate(src) or ""
            except Exception as e1:
                err = e1
        if not en or contains_cyrillic(en):
            try:
                en = MyMemoryTranslator(source='ru', target='en').translate(src) or ""
            except Exception as e2:
                err = e2
    except Exception as e:
        err = e
    # Fallback: if still empty or Cyrillic, prefix with EN marker to make it obvious
    if not en or contains_cyrillic(en):
        en = f"EN: {src}"
    save_draft({"text_ru": src, "text_en": en})
    # Include a small HTML comment with provider/fallback info to aid debugging in-page (not visible)
    info = "fallback" if en.startswith("EN: ") else "ok"
    html = f"<textarea id=\"text_en\" name=\"text_en\" rows=\"6\" cols=\"80\">{en}</textarea><!-- {info} -->"
    return HTMLResponse(html)

@router.post("/hashtags/suggest")
def suggest_hashtags(text: str = Form("")):
    # Always include project and theme tags
    tags = ["#Synthetica", "#indiedev", "#gamedev", "#tycoon", "#cyberpunk"]
    # Deduplicate while preserving order
    seen = set()
    ordered = []
    for t in tags:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    tag_str = " ".join(ordered)
    save_draft({"tags": tag_str})
    # Return an input prefilled so htmx can swap it in place
    return HTMLResponse(
        f"<input id=\"tags_input\" type=\"text\" name=\"tags\" value=\"{tag_str}\" style=\"width: 520px;\" />"
    )


@router.post("/draft/news")
def save_news_draft(
    text_ru: Optional[str] = Form(None),
    text_en: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    platforms: Optional[List[str]] = Form(None),
):
    payload = {}
    if text_ru is not None:
        payload["text_ru"] = text_ru
    if text_en is not None:
        payload["text_en"] = text_en
    if tags is not None:
        payload["tags"] = tags
    if scheduled_at is not None:
        payload["scheduled_at"] = scheduled_at
    if platforms is not None:
        payload["platforms"] = platforms
    if payload:
        save_draft(payload)
    return HTMLResponse("<span style='color:green'>Черновик сохранён</span>")


@router.post("/draft/clear")
def clear_news_draft():
    from app.services.draft_store import clear_draft
    clear_draft()
    return HTMLResponse("<span style='color:green'>Черновик очищен</span>")


@router.post("/uploads/draft_media")
def upload_draft_media(media: UploadFile = File(...), slot: Optional[str] = Form("normal")):
    os.makedirs('media', exist_ok=True)
    name_part = str(int(time.time()))
    safe_name = media.filename.replace('..', '').replace('/', '_').replace('\\', '_')
    media_path = os.path.join('media', f"{name_part}_{safe_name}")
    with open(media_path, 'wb') as f:
        f.write(media.file.read())
    # Build relative and URL
    rel = media_path.replace('\\', '/')
    rel = rel.split('media/')[-1]
    url = f"/media/{rel}"
    # Persist in draft under appropriate key
    slot_key = 'media_path'
    if slot == 'vertical':
        slot_key = 'media_vertical_path'
    elif slot == 'normal':
        slot_key = 'media_normal_path'
    save_draft({slot_key: rel})
    return {"media_url": url, "media_path": rel, "slot": slot}

@router.post("/briefs", response_model=BriefOut)
def create_brief(data: BriefIn, session: Session = Depends(get_session)):
    brief = Brief(user_id=1, type=data.type, text_ru=data.text_ru or "", text_en=data.text_en or "", tags_json=data.tags_json)
    session.add(brief)
    session.commit()
    session.refresh(brief)
    return BriefOut(id=brief.id)

@router.get("/posts")
def list_posts(limit: int = 10, session: Session = Depends(get_session)):
    rows = session.exec(select(Post).order_by(Post.created_at.desc()).limit(limit)).all()
    return rows

@router.get("/metrics/refresh")
def metrics_refresh(post_id: int, session: Session = Depends(get_session)):
    from app.jobs.tasks import refresh_metrics_for_post
    refresh_metrics_for_post.schedule(args=(post_id,), delay=0)
    return {"ok": True}

@router.post("/discord/webhook")
def save_discord_webhook(url: str = Form(...)):
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Empty webhook URL")
    save_settings({"discord_webhook_url": url})
    # Return small HTML snippet for htmx
    return HTMLResponse("<span style='color:green'>Discord webhook сохранён</span>")

@router.post("/discord/test")
def discord_send_test():
    url = load_settings().get("discord_webhook_url")
    if not url:
        return HTMLResponse("<span style='color:orange'>Discord webhook не настроен</span>", status_code=400)
    client = DiscordClient(url)
    try:
        mid, murl = client.send_message("Test from IndiePulse")
        link = f"<a href='{murl}' target='_blank'>сообщение</a>" if murl else ""
        return HTMLResponse(f"<span style='color:green'>Отправлено {link}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Ошибка: {str(exc)}</span>", status_code=500)


@router.post("/settings/save")
def save_settings_endpoint(
    vk_client_id: str = Form(""),
    vk_client_secret: str = Form(""),
    vk_group_id: str = Form(""),
    vk_redirect_uri: str = Form(""),
    vk_access_token: str = Form(""),
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
    save_settings(updates)
    # Ask htmx to reload the page so new values are visible
    return Response(content="", headers={"HX-Redirect": "/accounts"})


# VK OAuth: start and callback
@router.get("/accounts/connect/vk")
def vk_connect_start():
    cfg = load_settings()
    client_id = (cfg.get("vk_client_id") or "").strip()
    redirect_uri = (cfg.get("vk_redirect_uri") or "").strip()
    if not client_id or not redirect_uri:
        return HTMLResponse("<span style='color:orange'>Заполните Client ID и Redirect URI, затем сохраните настройки</span>", status_code=400)
    scope = "wall,offline,groups"
    auth_url = (
        "https://oauth.vk.com/authorize?"
        f"client_id={quote_plus(client_id)}&display=page&redirect_uri={quote_plus(redirect_uri)}"
        f"&response_type=code&v=5.199&scope={quote_plus(scope)}"
    )
    return HTMLResponse(f"<a href='{auth_url}' target='_blank'>Открыть VK авторизацию</a>")


@router.get("/accounts/callback/vk")
def vk_connect_callback(code: str):
    cfg = load_settings()
    client_id = cfg.get("vk_client_id")
    client_secret = cfg.get("vk_client_secret")
    redirect_uri = cfg.get("vk_redirect_uri")
    token_url = (
        "https://oauth.vk.com/access_token?"
        f"client_id={client_id}&client_secret={client_secret}&redirect_uri={redirect_uri}&code={code}"
    )
    with httpx.Client(timeout=10) as client:
        r = client.get(token_url)
        r.raise_for_status()
        data = r.json()
    access_token = data.get("access_token", "")
    user_id = str(data.get("user_id", ""))
    if not access_token:
        return HTMLResponse("<span style='color:red'>VK: не удалось получить токен</span>", status_code=400)
    save_settings({"vk_access_token": access_token, "vk_user_id": user_id})
    return HTMLResponse("<span style='color:green'>VK подключён</span>")


def _sanitize_group_id(group_raw: str) -> str:
    s = (group_raw or "").strip()
    s = s.lower().replace("club", "").replace("public", "").replace("event", "")
    if s.startswith("-"):
        s = s[1:]
    # keep only digits
    s = "".join(ch for ch in s if ch.isdigit())
    return s


@router.post("/vk/test_post")
def vk_test_post(text: str = Form("Test from IndiePulse")):
    cfg = load_settings()
    token = (cfg.get("vk_access_token") or "").strip()
    group_raw = (cfg.get("vk_group_id") or "").strip()
    if not token:
        return HTMLResponse("<span style='color:orange'>VK Access Token не задан</span>")
    group_id = _sanitize_group_id(group_raw)
    if not group_id:
        return HTMLResponse("<span style='color:orange'>VK Group ID не задан или некорректен</span>")
    owner_id = f"-{group_id}"  # negative for group wall
    params = {
        "owner_id": owner_id,
        "message": text,
        "from_group": 1,
        "v": "5.199",
        "access_token": token,
    }
    try:
        with httpx.Client(timeout=20) as client:
            r = client.post("https://api.vk.com/method/wall.post", data=params)
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Сетевой сбой: {str(exc)}</span>")
    if isinstance(data, dict) and "error" in data:
        err = data.get("error", {})
        msg = err.get("error_msg") or str(err)
        code = err.get("error_code")
        return HTMLResponse(f"<span style='color:red'>Ошибка VK [{code}]: {msg}</span>")
    post_id = (data.get("response") or {}).get("post_id") if isinstance(data, dict) else None
    link = f"https://vk.com/wall{owner_id}_{post_id}" if post_id else ""
    if not post_id:
        return HTMLResponse("<span style='color:orange'>Ответ VK без post_id</span>")
    return HTMLResponse(f"<span style='color:green'>Опубликовано: <a href='{link}' target='_blank'>ссылка</a></span>")


# X / Twitter OAuth 2.0 (PKCE simplified flow)
import base64, hashlib, secrets


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
        f"&scope={quote_plus(scope)}&state=state&code_challenge={quote_plus(challenge)}&code_challenge_method=S256"
    )
    return HTMLResponse(f"<a href='{url}' target='_blank'>Открыть X авторизацию</a>")


@router.get("/accounts/callback/x")
def x_callback(code: str):
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
        return HTMLResponse(f"<span style='color:red'>X OAuth error: {data}</span>")
    save_settings({
        "x_access_token": data.get("access_token", ""),
        "x_refresh_token": data.get("refresh_token", ""),
        "x_token_type": data.get("token_type", ""),
        "x_expires_at": int(data.get("expires_in", 0)),
    })
    return HTMLResponse("<span style='color:green'>X подключён</span>")


@router.post("/x/test_tweet")
def x_test_tweet(text: str = Form("Hello from IndiePulse")):
    cfg = load_settings()
    token = (cfg.get("x_access_token") or "").strip()
    if not token:
        return HTMLResponse("<span style='color:orange'>X не подключён</span>")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"text": text[:280]}
    with httpx.Client(timeout=15) as client:
        r = client.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
        data = r.json()
    if r.status_code >= 400:
        return HTMLResponse(f"<span style='color:red'>Ошибка X: {data}</span>")
    tid = (data.get("data") or {}).get("id")
    url = f"https://twitter.com/i/web/status/{tid}" if tid else ""
    return HTMLResponse(f"<span style='color:green'>Опубликовано: <a href='{url}' target='_blank'>ссылка</a></span>")


@router.get("/vk/implicit_url")
def vk_implicit_url():
    cfg = load_settings()
    client_id = (cfg.get("vk_client_id") or "").strip()
    if not client_id:
        return HTMLResponse("<span style='color:orange'>Укажите VK Client ID и сохраните настройки</span>", status_code=400)
    # Use VK's blank.html so the token is visible in the URL fragment for copy-paste
    redirect_uri = "https://oauth.vk.com/blank.html"
    scope = "wall,groups,offline"
    url = (
        "https://oauth.vk.com/authorize?"
        f"client_id={quote_plus(client_id)}&display=page&redirect_uri={quote_plus(redirect_uri)}"
        f"&response_type=token&v=5.199&scope={quote_plus(scope)}"
    )
    html = (
        f"<div>\n"
        f"<a href='{url}' target='_blank'>Открыть ссылку для получения Access Token (Implicit Flow)</a><br/>\n"
        f"После авторизации скопируйте значение access_token из адресной строки (после #access_token=...). Вставьте его в поле Access Token и сохраните.\n"
        f"</div>"
    )
    return HTMLResponse(html)


@router.post("/vk/check_token")
def vk_check_token(token: str = Form("")):
    cfg = load_settings()
    tok = (token or cfg.get("vk_access_token") or "").strip()
    if not tok:
        return HTMLResponse("<span style='color:orange'>Токен не задан</span>")
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get("https://api.vk.com/method/users.get", params={"access_token": tok, "v": "5.199"})
            data = r.json()
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Сетевой сбой: {str(exc)}</span>")
    if isinstance(data, dict) and "error" in data:
        err = data.get("error", {})
        return HTMLResponse(f"<span style='color:red'>Ошибка VK [{err.get('error_code')}]: {err.get('error_msg')}</span>")
    return HTMLResponse("<span style='color:green'>Токен валиден</span>")


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
