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
import io, zipfile
from app.services.video_editor import render_vertical_with_music
from app.services.video_editor import _ffmpeg_path, install_ffmpeg_win
from app.core.config import STEAM_APP_ID

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
    # Default hashtag set for preview/posting (exclude on Discord when publishing)
    tags = [
        "#Synthetica",
        "#UnrealEngine",
        "#Tycoon",
        "#Strategy",
        "#Gamedev",
        "#Indie",
    ]
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
    fb_ru_page_id: str = Form(""),
    fb_en_page_id: str = Form(""),
    fb_ru_page_access_token: str = Form(""),
    fb_en_page_access_token: str = Form(""),
    reddit_client_id: str = Form(""),
    reddit_client_secret: str = Form(""),
    reddit_username: str = Form(""),
    reddit_password: str = Form(""),
    reddit_subreddit_en: str = Form(""),
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
    if fb_ru_page_id.strip():
        updates["fb_ru_page_id"] = fb_ru_page_id.strip()
    if fb_en_page_id.strip():
        updates["fb_en_page_id"] = fb_en_page_id.strip()
    if fb_ru_page_access_token.strip():
        updates["fb_ru_page_access_token"] = fb_ru_page_access_token.strip()
    if fb_en_page_access_token.strip():
        updates["fb_en_page_access_token"] = fb_en_page_access_token.strip()
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

@router.post("/steam/export")
def steam_export(
    title_ru: str = Form(""),
    title_en: str = Form(""),
    body_ru: str = Form(""),
    body_en: str = Form(""),
    subtitle: str = Form(""),
    subtitle_en: str = Form(""),
    short_desc: str = Form(""),
    short_desc_en: str = Form(""),
    main_image: UploadFile = File(None),
    overlay_enabled: str = Form("1"),
    overlay_version: str = Form("VERSION 0.7.0"),
    overlay_sub: str = Form("FEATURE-COMPLETE UPDATE!"),
    ov_v_start: str = Form("#d9c8ff"),
    ov_v_end: str = Form("#7a5cf8"),
    ov_sub_start: str = Form("#ff9ac4"),
    ov_sub_end: str = Form("#ff5a9a"),
    ov_v_stroke_color: str = Form("#000000"),
    ov_v_stroke_px: int = Form(1),
    ov_sub_stroke_color: str = Form("#000000"),
    ov_sub_stroke_px: int = Form(1),
    ov_v_size: int = Form(72),
    ov_sub_size: int = Form(36),
    ov_v_weight: int = Form(700),
    ov_sub_weight: int = Form(600),
    sub_offset_y: int = Form(10),
    show_bg: str = Form("1"),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
    crop_scale: float = Form(1.0),
):
    # Create assets folder under media/steam_export/<timestamp>
    os.makedirs('media', exist_ok=True)
    base = str(int(time.time()))
    export_dir = os.path.join('media', 'steam_export', base)
    os.makedirs(export_dir, exist_ok=True)

    # Write text files with metadata
    ru_txt = f"[h1]{title_ru}[/h1]\n[b]{subtitle[:120]}[/b]\n[quote]{short_desc[:180]}[/quote]\n{body_ru}"
    en_txt = f"[h1]{title_en}[/h1]\n[b]{subtitle_en[:120]}[/b]\n[quote]{short_desc_en[:180]}[/quote]\n{body_en}"
    with open(os.path.join(export_dir, 'steam_ru.txt'), 'w', encoding='utf-8') as f:
        f.write(ru_txt)
    with open(os.path.join(export_dir, 'steam_en.txt'), 'w', encoding='utf-8') as f:
        f.write(en_txt)

    # Save main image and validate size 800x450; auto cover-crop to 800x450
    img_path = None
    if main_image is not None:
        try:
            from PIL import Image, ImageDraw, ImageFont
            data = main_image.file.read()
            img = Image.open(io.BytesIO(data)).convert('RGB')
            target_w, target_h = 800, 450
            # compute 16:9 crop region from original, with scale and offsets
            src_w, src_h = img.size
            aspect = target_w / target_h  # 16/9
            base_crop_w = min(src_w, int(src_h * aspect))
            # clamp crop scale 0.2..1.0
            try:
                cs = max(0.2, min(1.0, float(crop_scale)))
            except Exception:
                cs = 1.0
            crop_w = max(2, int(base_crop_w * cs))
            crop_h = int(crop_w / aspect)
            max_x = max(0, src_w - crop_w)
            max_y = max(0, src_h - crop_h)
            xx = int(max(0.0, min(1.0, float(crop_x_pct))) * max_x)
            yy = int(max(0.0, min(1.0, float(crop_y_pct))) * max_y)
            img = img.crop((xx, yy, xx + crop_w, yy + crop_h))
            img = img.resize((target_w, target_h), Image.LANCZOS)

            if (overlay_enabled or '').lower() in ('1','true','on','yes') and (overlay_version.strip() or overlay_sub.strip()):
                # Draw overlay: big version text and smaller subheader centered near bottom third
                draw = ImageDraw.Draw(img)
                # Fonts
                def _try_font(name: str, size: int):
                    try:
                        return ImageFont.truetype(name, size)
                    except Exception:
                        return None
                # Prefer Staatliches if installed on system or provided in working dir
                font_large = _try_font("Staatliches.ttf", ov_v_size) or _try_font("arialbd.ttf", ov_v_size) or _try_font("arial.ttf", ov_v_size) or ImageFont.load_default()
                font_small = _try_font("Staatliches.ttf", ov_sub_size) or _try_font("arialbd.ttf", ov_sub_size) or _try_font("arial.ttf", ov_sub_size) or ImageFont.load_default()

                # Gradient text function
                def draw_gradient_text(center_x: int, y: int, text: str, font, colors=("#c7b7ff","#6e48f6"), stroke=4, stroke_color="#000000"):
                    if not text:
                        return 0
                    bbox = draw.textbbox((0,0), text, font=font, stroke_width=stroke)
                    w = bbox[2]-bbox[0]
                    h = bbox[3]-bbox[1]
                    # build gradient image
                    from PIL import Image
                    grad = Image.new('RGB', (w, h), color=0)
                    c0, c1 = colors
                    # simple vertical gradient
                    r0,g0,b0 = ImageColor.getrgb(c0)
                    r1,g1,b1 = ImageColor.getrgb(c1)
                    pixels = grad.load()
                    for yy in range(h):
                        t = yy / max(1,h-1)
                        rr = int(r0 + (r1 - r0) * t)
                        gg = int(g0 + (g1 - g0) * t)
                        bb = int(b0 + (b1 - b0) * t)
                        for xx in range(w):
                            pixels[xx,yy] = (rr,gg,bb)
                    # stroke layer
                    if stroke and stroke > 0:
                        stroke_layer = Image.new('RGBA', (w, h), (0,0,0,0))
                        sd = ImageDraw.Draw(stroke_layer)
                        try:
                            sc = ImageColor.getrgb(stroke_color or '#000000')
                        except Exception:
                            sc = (0,0,0)
                        sd.text((0,0), text, font=font, fill=sc+(255,), stroke_width=stroke, stroke_fill=sc)
                        img.paste(stroke_layer, (center_x - w//2, y), stroke_layer)
                    # text mask
                    mask = Image.new('L', (w, h), 0)
                    md = ImageDraw.Draw(mask)
                    md.text((0,0), text, font=font, fill=255, stroke_width=stroke, stroke_fill=(0))
                    # position
                    x = center_x - w//2
                    img.paste(grad, (x, y), mask)
                    return h

                from PIL import ImageColor
                cx = target_w//2
                # Drop shadow backdrop
                shadow = Image.new('RGBA', (target_w, target_h), (0,0,0,0))
                sd = ImageDraw.Draw(shadow)
                # Draw shadow rectangles for readability
                sd.rectangle([0, int(target_h*0.6), target_w, target_h], fill=(0,0,0,90))
                img = Image.alpha_composite(img.convert('RGBA'), shadow).convert('RGB')
                draw = ImageDraw.Draw(img)

                title_y = 290
                # Optional dark band
                if (show_bg or '1').lower() in ('1','true','on','yes'):
                    draw.rectangle([0, max(0, title_y-20), target_w, target_h], fill=(0,0,0,90))
                # Heavier weight -> slightly thicker stroke
                v_weight_extra = 1 if (ov_v_weight or 700) >= 600 else 0
                if (ov_v_weight or 700) >= 800:
                    v_weight_extra += 1
                v_stroke = max(0, int(ov_v_stroke_px or 1) + v_weight_extra)
                h_big = draw_gradient_text(cx, title_y, overlay_version.strip(), font_large, colors=(ov_v_start or "#d9c8ff", ov_v_end or "#7a5cf8"), stroke=v_stroke, stroke_color=ov_v_stroke_color or "#000000")
                y_small = title_y + h_big + max(0, int(sub_offset_y or 10))
                # Smaller line with gradient as well
                sub_text = overlay_sub.strip()
                if sub_text:
                    s_weight_extra = 1 if (ov_sub_weight or 600) >= 600 else 0
                    if (ov_sub_weight or 600) >= 800:
                        s_weight_extra += 1
                    s_stroke = max(0, int(ov_sub_stroke_px or 1) + s_weight_extra)
                    draw_gradient_text(cx, y_small, sub_text, font_small, colors=(ov_sub_start or '#ff9ac4', ov_sub_end or '#ff5a9a'), stroke=s_stroke, stroke_color=ov_sub_stroke_color or "#000000")

            img_path = os.path.join(export_dir, 'main_800x450.jpg')
            img.save(img_path, format='JPEG', quality=92)
        except Exception as exc:
            return HTMLResponse(f"<span style='color:red'>Image processing failed: {str(exc)}</span>", status_code=400)

    # Respond with folder details and a helper link
    return {
        "ok": True,
        "export_dir": export_dir.replace('\\','/'),
        "steam_create_url": f"https://steamcommunity.com/games/{STEAM_APP_ID}/partnerevents/create"
    }

@router.get("/steam/open_dir")
def steam_open_dir(path: str):
    # Open directory in explorer (Windows only)
    try:
        p = (path or '').strip()
        if not p:
            return HTMLResponse("<span style='color:red'>Empty path</span>", status_code=400)
        full = os.path.abspath(p)
        if not os.path.isdir(full):
            return HTMLResponse("<span style='color:red'>Not a directory</span>", status_code=400)
        if os.name == 'nt':
            import subprocess
            subprocess.Popen(["explorer", full])
            return {"ok": True}
        else:
            return HTMLResponse("<span style='color:orange'>Open folder supported on Windows only</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Open failed: {str(exc)}</span>", status_code=500)

@router.post("/video/preview_frame")
def video_preview_frame(
    video: UploadFile = File(...),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
):
    """Generate a preview frame showing the crop area"""
    os.makedirs('render', exist_ok=True)
    os.makedirs('media', exist_ok=True)
    
    base = str(int(time.time()))
    safe_video_name = video.filename.replace('\\','/').split('/')[-1]
    video_path = os.path.join('render', f"{base}_src_{safe_video_name}")
    
    with open(video_path, 'wb') as f:
        f.write(video.file.read())
    
    # Generate preview frame with crop overlay
    preview_path = os.path.join('media', f"{base}_preview.jpg")
    
    try:
        ffmpeg = _ffmpeg_path()
        
        # Extract a frame and add crop overlay
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-vf", f"select=eq(n\\,0),scale=-2:480,drawbox=x=(iw-270)*{crop_x_pct}:y=(ih-480)*{crop_y_pct}:w=270:h=480:color=red@0.5:t=fill",
            "-frames:v", "1",
            preview_path
        ]
        
        import subprocess
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg preview failed: {proc.stderr[-500:]}")
        
        rel = preview_path.replace('\\','/')
        return {"preview_url": f"/media/{os.path.basename(rel)}"}
        
    except Exception as e:
        # Provide a helpful hint for missing ffmpeg
        return {"error": str(e), "hint": "FFmpeg is required. Click 'Install FFmpeg' on the News page to auto-install on Windows, or install from ffmpeg.org and add to PATH."}

@router.post("/video/render_vertical")
def video_render_vertical(
    video: UploadFile = File(...),
    music: UploadFile = File(None),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
    music_offset_s: float = Form(0.0),
    fade_in_s: float = Form(1.0),
    fade_out_s: float = Form(1.0),
    start_time: float = Form(0.0),
    end_time: float = Form(0.0),
    zoom: float = Form(1.0),
):
    os.makedirs('render', exist_ok=True)
    os.makedirs('media', exist_ok=True)
    # Save inputs
    base = str(int(time.time()))
    # Clean filename for safe file operations
    safe_video_name = video.filename.replace('\\','/').split('/')[-1]
    video_path = os.path.join('render', f"{base}_src_{safe_video_name}")
    with open(video_path, 'wb') as f:
        f.write(video.file.read())
    music_path = None
    if music is not None:
        safe_music_name = music.filename.replace('\\','/').split('/')[-1]
        music_path = os.path.join('render', f"{base}_music_{safe_music_name}")
        with open(music_path, 'wb') as f:
            f.write(music.file.read())
    # Write final output into media so it is web-accessible
    out_path = os.path.join('media', f"{base}_vertical.mp4")
    render_vertical_with_music(
        input_video=video_path,
        output_video=out_path,
        crop_x_pct=crop_x_pct,
        crop_y_pct=crop_y_pct,
        music_path=music_path,
        music_offset_s=music_offset_s,
        fade_in_s=fade_in_s,
        fade_out_s=fade_out_s,
        start_time=start_time,
        end_time=end_time,
        zoom=zoom,
    )
    # Return relative path for preview/download
    rel = out_path.replace('\\','/')
    # Hidden field expects a path relative to media folder (filename)
    return {"output_path": rel, "output_rel": os.path.basename(rel), "output_url": f"/media/{os.path.basename(rel)}"}


@router.post("/video/install_ffmpeg")
def video_install_ffmpeg():
    try:
        # Try resolving; if not present, run installer for Windows
        try:
            path = _ffmpeg_path()
        except Exception:
            path = install_ffmpeg_win()
        return {"ok": True, "path": path}
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>FFmpeg install failed: {str(exc)}</span>", status_code=500)