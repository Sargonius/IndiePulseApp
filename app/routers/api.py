from fastapi import APIRouter, Depends, HTTPException, Form, Request
from fastapi.responses import HTMLResponse, Response, RedirectResponse
from pydantic import BaseModel
from typing import List, Optional
from sqlmodel import select
from app.db.session import get_session
from sqlmodel import Session
from app.models.models import Brief, Post, Project, now_ts
from app.services.discord_client import DiscordClient
from app.services.settings import load_settings, save_settings
import httpx
from urllib.parse import quote_plus
from app.services.draft_store import save_draft, load_draft
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
from app.services.facebook_client import FacebookClient
from fastapi.templating import Jinja2Templates
from fastapi import Query

router = APIRouter(prefix="/api")
templates_api = Jinja2Templates(directory='app/templates')
@router.get("/analytics/table", response_class=HTMLResponse)
def analytics_table(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    from app.models.models import Post as _Post
    rows = session.exec(select(_Post).order_by(_Post.created_at.desc()).limit(100)).all()
    # Reuse pages enrichment to attach preview/thumb and mapped metrics
    try:
        from app.routers.pages import _enrich_posts as _ep
        data = _ep(rows)
    except Exception:
        data = []
    return templates_api.TemplateResponse('partials/analytics_table.html', {'request': request, 'posts': data})


@router.post("/analytics/refresh", response_class=HTMLResponse)
def analytics_refresh(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    from app.models.models import Post as _Post
    rows = session.exec(select(_Post).order_by(_Post.created_at.desc()).limit(100)).all()
    ids = [p.id for p in rows]
    # Run metrics refresh synchronously
    try:
        from app.jobs.tasks import refresh_metrics_for_post as _refresh
        for pid in ids:
            try:
                _refresh.call_local(pid)
            except Exception:
                pass
    except Exception:
        pass
    # Return fresh table
    return analytics_table(request, session)


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
@router.get("/translate/test_openai")
def test_openai():
    try:
        cfg = load_settings()
        api_key = (cfg.get("openai_api_key") or "").strip()
        model = (cfg.get("openai_model") or "gpt-4o-mini").strip()
        if not api_key:
            return {"ok": False, "error": "no_api_key"}
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": "You translate RU to EN."},
                {"role": "user", "content": "Переведи на английский: Привет мир"},
            ],
        }
        with httpx.Client(timeout=15) as client:
            r = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
            data = {"status": r.status_code}
            try:
                data["json"] = r.json()
            except Exception:
                data["text"] = r.text[:400]
            return {"ok": r.status_code < 400, "response": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/translate")
def translate(ru_text: Optional[str] = Form(None), text_ru: Optional[str] = Form(None)):
    src = (ru_text or text_ru or "").strip()
    if not src:
        return HTMLResponse("<textarea id=\"text_en\" name=\"text_en\" rows=\"6\" cols=\"80\"></textarea>")
    def contains_cyrillic(s: str) -> bool:
        return any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in s)
    en = ""
    err = None
    provider = ""
    # 1) Try OpenAI if configured
    try:
        cfg = load_settings()
        api_key = (cfg.get("openai_api_key") or "").strip()
        model = (cfg.get("openai_model") or "gpt-4o-mini").strip()
        if api_key and model:
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "temperature": 0.2,
                "messages": [
                    {"role": "system", "content": "You are a professional localization translator. Translate Russian to natural, idiomatic English. Preserve Steam markup tags (e.g., [h1], [b], [url], [list]) exactly as-is. Do not add commentary; return only the translated text."},
                    {"role": "user", "content": f"Translate to English, preserving markup:\n\n{src}"},
                ],
            }
            with httpx.Client(timeout=20) as client:
                r = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                if r.status_code < 400:
                    data = r.json()
                    en = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    if en:
                        provider = f"openai:{model}"
                    else:
                        try:
                            logger.warning("openai_empty_response model=%s raw=%s", model, str(data)[:400])
                        except Exception:
                            pass
                else:
                    try:
                        logger.warning("openai_http_error status=%s model=%s body=%s", r.status_code, model, r.text[:400])
                    except Exception:
                        pass
    except Exception as e_oa:
        err = e_oa
    # 2) Fallback chain via deep_translator
    if not en or contains_cyrillic(en):
        try:
            from deep_translator import GoogleTranslator, MyMemoryTranslator, LibreTranslator
            for host in (
                'https://libretranslate.de',
                'https://translate.astian.org',
            ):
                try:
                    en = LibreTranslator(source='ru', target='en', host=host).translate(src) or ""
                    if en and not contains_cyrillic(en):
                        provider = f"libre:{host}"
                        break
                except Exception as e_lt:
                    err = e_lt
            if not en or contains_cyrillic(en):
                try:
                    en = GoogleTranslator(source='ru', target='en').translate(src) or ""
                    if en and not contains_cyrillic(en):
                        provider = "google"
                except Exception as e1:
                    err = e1
            if not en or contains_cyrillic(en):
                try:
                    en = MyMemoryTranslator(source='ru', target='en').translate(src) or ""
                    if en and not contains_cyrillic(en):
                        provider = "mymemory"
                except Exception as e2:
                    err = e2
        except Exception as e:
            err = e
    # 3) Final fallback: echo with EN: prefix
    if not en or contains_cyrillic(en):
        en = f"EN: {src}"
        if not provider:
            provider = "fallback"
    save_draft({"text_ru": src, "text_en": en})
    info = provider or ("fallback" if en.startswith("EN: ") else "ok")
    try:
        logger.info(f"translate provider={info} in_len={len(src)} out_len={len(en)}")
    except Exception:
        pass
    html = f"<textarea id=\"text_en\" name=\"text_en\" rows=\"6\" cols=\"80\" data-translator=\"{info}\">{en}</textarea><!-- provider: {info} -->"
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
    reddit_sentences: Optional[int] = Form(None),
    yt_title: Optional[str] = Form(None),
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
    if reddit_sentences is not None:
        try:
            n = max(1, min(10, int(reddit_sentences)))
            payload["reddit_sentences"] = n
        except Exception:
            pass
    if yt_title is not None:
        payload["yt_title"] = yt_title
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
    # Persist in draft; support gallery append for slot == 'gallery'
    if slot == 'gallery':
        state = load_draft()
        gallery = list(state.get('gallery_normal') or [])
        gallery.append(rel)
        save_draft({"gallery_normal": gallery})
        return {"media_url": url, "media_path": rel, "slot": slot, "index": len(gallery) - 1}
    else:
        slot_key = 'media_path'
        if slot == 'vertical':
            slot_key = 'media_vertical_path'
        elif slot == 'normal':
            slot_key = 'media_normal_path'
        save_draft({slot_key: rel})
        return {"media_url": url, "media_path": rel, "slot": slot}


@router.post("/gallery/reorder")
def gallery_reorder(order: str = Form("")):
    # order: comma-separated list of rel paths
    items = [x.strip() for x in (order or '').split(',') if x.strip()]
    save_draft({"gallery_normal": items})
    return HTMLResponse("<span style='color:green'>Галерея обновлена</span>")


@router.post("/gallery/remove")
def gallery_remove(item: str = Form("")):
    state = load_draft()
    cur = list(state.get('gallery_normal') or [])
    new_list = [x for x in cur if x != (item or '').strip()]
    save_draft({"gallery_normal": new_list})
    return {"ok": True, "count": len(new_list)}

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
    # Immediate schedule for generic metrics
    refresh_metrics_for_post.schedule(args=(post_id,), delay=0)
    # Inline poll for Reddit visibility if applicable
    from app.models.models import Post
    post = session.get(Post, post_id)
    if not post:
        return {"ok": False, "error": "post_not_found"}
    plat = str(post.platform)
    # Generic: if already has URL or post_id, consider it done
    from app.models.models import PostStatusEnum
    if post.status == PostStatusEnum.running and (post.platform_url or post.platform_post_id):
        post.status = PostStatusEnum.done
        session.add(post)
        session.commit()
        return {"ok": True, "status": "done"}

    if plat.endswith('reddit') and post.platform_post_id:
        try:
            from app.services.settings import load_settings
            from app.services.reddit_client import RedditClient
            cfg = load_settings()
            cid = (cfg.get('reddit_client_id') or '').strip()
            csec = (cfg.get('reddit_client_secret') or '').strip()
            uname = (cfg.get('reddit_username') or '').strip()
            pwd = (cfg.get('reddit_password') or '').strip()
            rc = RedditClient(cid, csec, uname, pwd)
            # If visible (not removed/spam), keep URL; mark done
            # PRAW submission by id: full name requires t3_ prefix
            fullname = post.platform_post_id if post.platform_post_id.startswith('t3_') else f"t3_{post.platform_post_id}"
            subm = rc.reddit.submission(id=fullname.split('_')[-1])
            # If available, update platform_url and mark done
            post.platform_url = subm.permalink and f"https://reddit.com{subm.permalink}" or post.platform_url
            from app.models.models import PostStatusEnum
            post.status = PostStatusEnum.done
            session.add(post)
            session.commit()
        except Exception as exc:
            # Non-fatal; return info
            return {"ok": True, "note": f"reddit_poll_error: {str(exc)}"}
    return {"ok": True}


@router.post("/posts/remove")
def remove_post(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    from app.models.models import Post
    pid = id or id_q
    if not pid:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    p = session.get(Post, pid)
    if not p:
        return HTMLResponse("<span style='color:red'>post_not_found</span>", status_code=404)
    session.delete(p)
    session.commit()
    # Return refreshed posts partial
    from sqlmodel import select as _select
    rows = session.exec(_select(Post).order_by(Post.created_at.desc()).limit(50)).all()
    # Reuse same enrichment from pages.py to avoid duplication would require import; simple inline copy here
    data = []
    import json as _json
    for p2 in rows:
        metrics = {}
        try:
            if getattr(p2, 'metrics_json', None):
                metrics = _json.loads(p2.metrics_json) or {}
        except Exception:
            metrics = {}
        def pick(*keys):
            for k in keys:
                if metrics.get(k) is not None:
                    return metrics.get(k)
            return None
        data.append({
            'id': p2.id,
            'platform_text': (p2.platform.value if hasattr(p2.platform, 'value') else str(p2.platform)),
            'status_text': (p2.status.value if hasattr(p2.status, 'value') else str(p2.status)),
            'brief_id': getattr(p2, 'brief_id', None),
            'scheduled_at': getattr(p2, 'scheduled_at', None),
            'published_at': getattr(p2, 'published_at', None),
            'platform_url': getattr(p2, 'platform_url', None),
            'm_like': pick('like', 'likes'),
            'm_repost': pick('retweet', 'reposts'),
            'm_reply': pick('reply', 'comments'),
            'm_quote': pick('quote', 'views'),
            'tags': getattr(p2, 'brief', None) and getattr(p2.brief, 'tags_json', None),
        })
    return templates_api.TemplateResponse('partials/posts_table.html', {'request': request, 'posts': data})


@router.post("/posts/update_schedule")
def update_post_schedule(request: Request, id: Optional[int] = Form(None), scheduled_at: Optional[str] = Form(None), session: Session = Depends(get_session)):
    from app.models.models import Post, Job, JobKindEnum, JobStatusEnum, PostStatusEnum
    import json as _json
    if not id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    post = session.get(Post, id)
    if not post:
        return HTMLResponse("<span style='color:red'>post_not_found</span>", status_code=404)
    # Parse input (datetime-local sends e.g. 2025-08-31T14:30)
    new_dt = None
    s = (scheduled_at or '').strip()
    if s:
        try:
            from datetime import datetime as _dt
            new_dt = _dt.fromisoformat(s)
        except Exception:
            return HTMLResponse("<span style='color:red'>bad_datetime_format</span>", status_code=400)
    post.scheduled_at = new_dt
    session.add(post)
    session.commit()
    # If post is queued, enqueue a new publish job at the new time (or immediately if None/past)
    try:
        from datetime import datetime as _dt
        if post.status == PostStatusEnum.queued:
            payload = {"post_id": post.id}
            job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
            session.add(job)
            session.commit()
            session.refresh(job)
            from app.jobs.tasks import publish_news_post
            delay_s = 0
            if new_dt is not None:
                target = new_dt
                if getattr(target, 'tzinfo', None) is not None:
                    try:
                        target = target.replace(tzinfo=None)
                    except Exception:
                        pass
                delay_s = int(max(0, (target - _dt.now()).total_seconds()))
            publish_news_post.schedule(args=(job.id, post.id), delay=delay_s)
    except Exception:
        pass
    # Return refreshed posts table
    from sqlmodel import select as _select
    rows = session.exec(_select(Post).order_by(Post.created_at.desc()).limit(50)).all()
    # Enrich like above
    data = []
    import json as _json2
    for p2 in rows:
        metrics = {}
        try:
            if getattr(p2, 'metrics_json', None):
                metrics = _json2.loads(p2.metrics_json) or {}
        except Exception:
            metrics = {}
        def pick(*keys):
            for k in keys:
                if metrics.get(k) is not None:
                    return metrics.get(k)
            return None
        data.append({
            'id': p2.id,
            'platform_text': (p2.platform.value if hasattr(p2.platform, 'value') else str(p2.platform)),
            'status_text': (p2.status.value if hasattr(p2.status, 'value') else str(p2.status)),
            'brief_id': getattr(p2, 'brief_id', None),
            'scheduled_at': getattr(p2, 'scheduled_at', None),
            'published_at': getattr(p2, 'published_at', None),
            'platform_url': getattr(p2, 'platform_url', None),
            'm_like': pick('like', 'likes'),
            'm_repost': pick('retweet', 'reposts'),
            'm_reply': pick('reply', 'comments'),
            'm_quote': pick('quote', 'views'),
            'tags': getattr(p2, 'brief', None) and getattr(p2.brief, 'tags_json', None),
        })
    return templates_api.TemplateResponse('partials/posts_table.html', {'request': request, 'posts': data})


@router.post("/posts/refresh_metrics")
def refresh_post_metrics(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    pid = id or id_q
    if not pid:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.jobs.tasks import refresh_metrics_for_post
    refresh_metrics_for_post.schedule(args=(pid,), delay=0)
    # Return refreshed table after scheduling
    from app.models.models import Post
    from sqlmodel import select as _select
    rows = session.exec(_select(Post).order_by(Post.created_at.desc()).limit(50)).all()
    # minimal: just trigger client-side refresh; but we return table to keep UX consistent
    # Enrich like above
    data = []
    import json as _json
    for p2 in rows:
        metrics = {}
        try:
            if getattr(p2, 'metrics_json', None):
                metrics = _json.loads(p2.metrics_json) or {}
        except Exception:
            metrics = {}
        def pick(*keys):
            for k in keys:
                if metrics.get(k) is not None:
                    return metrics.get(k)
            return None
        data.append({
            'id': p2.id,
            'platform_text': (p2.platform.value if hasattr(p2.platform, 'value') else str(p2.platform)),
            'status_text': (p2.status.value if hasattr(p2.status, 'value') else str(p2.status)),
            'brief_id': getattr(p2, 'brief_id', None),
            'scheduled_at': getattr(p2, 'scheduled_at', None),
            'published_at': getattr(p2, 'published_at', None),
            'platform_url': getattr(p2, 'platform_url', None),
            'm_like': pick('like', 'likes'),
            'm_repost': pick('retweet', 'reposts'),
            'm_reply': pick('reply', 'comments'),
            'm_quote': pick('quote', 'views'),
            'tags': getattr(p2, 'brief', None) and getattr(p2.brief, 'tags_json', None),
        })
    return templates_api.TemplateResponse('partials/posts_table.html', {'request': request, 'posts': data})


@router.post("/system/restart")
def system_restart():
    """Request a full app restart by invoking restart.bat (Windows)."""
    try:
        import subprocess, os
        script = os.path.abspath('restart.bat')
        if not os.path.exists(script):
            return HTMLResponse("<span style='color:red'>restart.bat not found</span>", status_code=500)
        # Use PowerShell to detach and hide window
        cmd = [
            'powershell', '-NoProfile', '-Command',
            f"Start-Process -FilePath '{script}' -WorkingDirectory '{os.getcwd()}' -WindowStyle Hidden"
        ]
        creationflags = 0
        try:
            import subprocess as _sp
            if hasattr(_sp, 'DETACHED_PROCESS'): creationflags |= _sp.DETACHED_PROCESS
            if hasattr(_sp, 'CREATE_NEW_PROCESS_GROUP'): creationflags |= _sp.CREATE_NEW_PROCESS_GROUP
        except Exception:
            pass
        subprocess.Popen(cmd, creationflags=creationflags)
        # Ask htmx to refresh the page so the UI reconnects after the restart
        return HTMLResponse(
            "<span style='color:orange'>Restarting... (logs in logs/server.* and logs/worker.*)</span>",
            headers={"HX-Refresh": "true"}
        )
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Restart failed: {str(exc)}</span>", status_code=500)


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
        return HTMLResponse(f"<span style='color:red'>Toggle failed: {str(exc)}</span>", status_code=500)


@router.get("/system/autoreload_status")
def system_autoreload_status():
    import os
    enabled = os.path.exists('.autoreload')
    return HTMLResponse(_render_autoreload_button_html(enabled))


@router.post("/telegram/test")
def telegram_test(message: str = Form("Test from IndiePulse")):
    try:
        cfg = load_settings()
        token = (cfg.get("tg_bot_token") or "").strip()
        chat_id = (cfg.get("tg_chat_id_ru") or "").strip()
        if not token or not chat_id:
            return HTMLResponse("<span style='color:orange'>Telegram не настроен</span>")
        import httpx
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        data = {"chat_id": chat_id, "text": message}
        with httpx.Client(timeout=15) as client:
            r = client.post(url, data=data)
            jd = r.json()
        if r.status_code >= 400 or not jd.get("ok"):
            return HTMLResponse(f"<span style='color:red'>Ошибка Telegram: {jd}</span>")
        mid = ((jd.get("result") or {}).get("message_id"))
        return HTMLResponse(f"<span style='color:green'>Отправлено (message_id={mid})</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Ошибка: {str(exc)}</span>", status_code=500)


@router.post("/telegram/check")
def telegram_check():
    try:
        cfg = load_settings()
        token = (cfg.get("tg_bot_token") or "").strip()
        chat_id = (cfg.get("tg_chat_id_ru") or "").strip()
        if not token:
            return HTMLResponse("<span style='color:orange'>Нет Bot Token</span>")
        import httpx
        with httpx.Client(timeout=10) as client:
            me = client.get(f"https://api.telegram.org/bot{token}/getMe")
            mjs = me.json()
            if not mjs.get("ok"):
                return HTMLResponse(f"<span style='color:red'>getMe: {mjs}</span>")
            chat_status = ""
            if chat_id:
                ch = client.get(f"https://api.telegram.org/bot{token}/getChat", params={"chat_id": chat_id})
                cjs = ch.json()
                chat_status = " ok" if cjs.get("ok") else f" error: {cjs}"
        return HTMLResponse(f"<span style='color:green'>Bot OK.{chat_status}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Ошибка: {str(exc)}</span>")

@router.post("/discord/webhook")
def save_discord_webhook(url: str = Form(...)):
    url = (url or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Empty webhook URL")
    save_settings({"discord_webhook_url": url})
    # Return small HTML snippet for htmx
    return HTMLResponse("<span style='color:green'>Discord webhook сохранён</span>")

@router.post("/discord/webhooks/save")
def save_discord_webhooks(url_ru: str = Form(""), url_en: str = Form("")):
    updates = {}
    if (url_ru or "").strip():
        updates["discord_webhook_ru"] = url_ru.strip()
    if (url_en or "").strip():
        updates["discord_webhook_en"] = url_en.strip()
    if updates:
        save_settings(updates)
    return HTMLResponse("<span style='color:green'>Webhooks updated</span>")

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
	fb_app_id: str = Form(""),
	fb_app_secret: str = Form(""),
	fb_ru_page_id: str = Form(""),
	fb_en_page_id: str = Form(""),
	fb_ru_page_access_token: str = Form(""),
	fb_en_page_access_token: str = Form(""),
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
	# UI language
	if ui_language.strip():
		lang = ui_language.strip().lower()
		if lang in ("en", "ru"):
			updates["ui_language"] = lang
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
    scope = "wall,offline,groups,photos,video"
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
    """Return a valid X access token, refreshing if needed. Raises on failure."""
    from time import time as _now
    cfg = load_settings()
    token = (cfg.get("x_access_token") or "").strip()
    refresh = (cfg.get("x_refresh_token") or "").strip()
    client_id = (cfg.get("x_client_id") or "").strip()
    client_secret = (cfg.get("x_client_secret") or "").strip()
    exp_ts = int(cfg.get("x_expires_at_ts") or 0)
    # If token missing or expiring within 60s, refresh
    if not token or (exp_ts and exp_ts - int(_now()) < 60):
        if not (refresh and client_id and client_secret):
            raise RuntimeError("x_refresh_unavailable")
        token_url = "https://api.twitter.com/2/oauth2/token"
        data = {
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": client_id,
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        import base64 as _b64
        basic = _b64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
        with httpx.Client(timeout=15) as client:
            r = client.post(token_url, data=data, headers=headers)
            jd = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"x_refresh_failed: {jd}")
        new_token = jd.get("access_token", "")
        new_refresh = jd.get("refresh_token", refresh)
        expires_in = int(jd.get("expires_in", 0))
        from time import time as _now2
        save_settings({
            "x_access_token": new_token,
            "x_refresh_token": new_refresh,
            "x_token_type": jd.get("token_type", cfg.get("x_token_type")),
            "x_expires_at": expires_in,
            "x_expires_at_ts": int(_now2()) + expires_in,
        })
        token = new_token
    return token


@router.post("/x/test_tweet")
def x_test_tweet(text: str = Form("Hello from IndiePulse")):
    # Ensure valid token
    try:
        token = _x_ensure_token()
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>X token error: {str(exc)}</span>")
    if not token:
        return HTMLResponse("<span style='color:orange'>X не подключён</span>")
    # Validate token scopes and status
    try:
        with httpx.Client(timeout=10) as client:
            me = client.get("https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {token}"})
            info = me.json()
            if me.status_code >= 400:
                return HTMLResponse(f"<span style='color:red'>X token invalid: {info}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>X check failed: {str(exc)}</span>")
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


# TikTok OAuth 2.0
@router.get("/accounts/connect/tiktok")
def tiktok_connect_start():
    """Start TikTok OAuth flow."""
    cfg = load_settings()
    client_key = (cfg.get("tiktok_client_key") or "").strip()
    if not client_key:
        return HTMLResponse("<span style='color:orange'>Configure TikTok Client Key in settings</span>")
    
    # TikTok OAuth URL (adjust redirect_uri as needed)
    redirect_uri = "http://127.0.0.1:8000/api/accounts/callback/tiktok"
    scope = "user.info.basic,video.publish,video.upload"
    state = "tiktok_oauth_state"  # In production, use random state and store it
    
    # Save state for verification
    save_settings({"tiktok_oauth_state": state})
    
    auth_url = (
        f"https://www.tiktok.com/v2/auth/authorize?"
        f"client_key={quote_plus(client_key)}&"
        f"response_type=code&"
        f"scope={quote_plus(scope)}&"
        f"redirect_uri={quote_plus(redirect_uri)}&"
        f"state={state}"
    )
    
    return HTMLResponse(f"<a href='{auth_url}' target='_blank'>Open TikTok Authorization</a>")


@router.get("/accounts/callback/tiktok")
def tiktok_callback(code: str, state: str = ""):
    """Handle TikTok OAuth callback."""
    cfg = load_settings()
    client_key = cfg.get("tiktok_client_key", "")
    client_secret = cfg.get("tiktok_client_secret", "")
    
    if not client_key or not client_secret:
        return HTMLResponse("<span style='color:red'>TikTok credentials not configured</span>", status_code=400)
    
    # Verify state (basic check)
    saved_state = cfg.get("tiktok_oauth_state", "")
    if state != saved_state:
        return HTMLResponse("<span style='color:red'>Invalid OAuth state</span>", status_code=400)
    
    # Exchange code for access token
    token_url = "https://open.tiktokapis.com/v2/oauth/token/"
    redirect_uri = "http://127.0.0.1:8000/api/accounts/callback/tiktok"
    
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
            return HTMLResponse(f"<span style='color:red'>No access token: {data}</span>", status_code=400)
        
        from time import time as _now
        save_settings({
            "tiktok_access_token": access_token,
            "tiktok_refresh_token": refresh_token,
            "tiktok_token_expires_at": int(_now()) + expires_in,
        })
        
        return HTMLResponse("<span style='color:green'>TikTok connected successfully!</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>TikTok OAuth error: {str(exc)}</span>", status_code=500)


@router.post("/tiktok/test")
def tiktok_test():
    """Test TikTok API connection (check token validity)."""
    cfg = load_settings()
    access_token = (cfg.get("tiktok_access_token") or "").strip()
    
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
            return HTMLResponse(f"<span style='color:red'>TikTok API error: {data}</span>")
        
        user_info = data.get("data", {}).get("user", {})
        return HTMLResponse(f"<span style='color:green'>TikTok connected. User: {user_info.get('open_id', 'unknown')}</span>")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>TikTok test failed: {str(exc)}</span>")


@router.get("/vk/implicit_url")
def vk_implicit_url():
    cfg = load_settings()
    client_id = (cfg.get("vk_client_id") or "").strip()
    if not client_id:
        return HTMLResponse("<span style='color:orange'>Укажите VK Client ID и сохраните настройки</span>", status_code=400)
    # Use VK's blank.html so the token is visible in the URL fragment for copy-paste
    redirect_uri = "https://oauth.vk.com/blank.html"
    scope = "wall,groups,offline,photos,video"
    url = (
        "https://oauth.vk.com/authorize?"
        f"client_id={quote_plus(client_id)}&display=page&redirect_uri={quote_plus(redirect_uri)}"
        f"&response_type=token&v=5.199&scope={quote_plus(scope)}"
    )
    html = (
        f"<div style='padding:12px; background:#fff3cd; border:1px solid #ffc107; border-radius:4px;'>\n"
        f"<p style='margin:0 0 8px 0; font-weight:600; color:#856404;'>⚠️ ВАЖНО: Токен должен иметь права</p>\n"
        f"<p style='margin:0 0 12px 0; font-family:monospace; background:#fff; padding:6px; border-radius:3px;'>{scope}</p>\n"
        f"<a class='btn btn-primary' href='{url}' target='_blank' style='display:inline-block; margin-bottom:8px;'>Открыть ссылку для получения Access Token</a><br/>\n"
        f"<p style='margin:8px 0 0 0; font-size:13px; color:#666;'>После авторизации скопируйте значение <b>access_token</b> из адресной строки (после #access_token=...). Вставьте его в поле <b>Access Token</b> и <b>User Access Token</b> (оба поля!) и сохраните.</p>\n"
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
            # Проверяем валидность токена и получаем информацию о правах
            r = client.get("https://api.vk.com/method/users.get", params={"access_token": tok, "v": "5.199"})
            data = r.json()
            
            if isinstance(data, dict) and "error" in data:
                err = data.get("error", {})
                return HTMLResponse(f"<span style='color:red'>Ошибка VK [{err.get('error_code')}]: {err.get('error_msg')}</span>")
            
            # Проверяем права токена через apps.getScopes
            r2 = client.get("https://api.vk.com/method/apps.getScopes", params={
                "access_token": tok,
                "type": "user",
                "v": "5.199"
            })
            scopes_data = r2.json()
            
            scopes_info = ""
            if isinstance(scopes_data, dict) and "response" in scopes_data:
                items = scopes_data.get("response", {}).get("items", [])
                scope_names = [item.get("name") for item in items if item.get("name")]
                
                required = {"photos", "wall", "groups", "video", "offline"}
                has_scopes = set(scope_names)
                missing = required - has_scopes
                
                if missing:
                    scopes_info = f"<br/><span style='color:#ff9800;'>⚠️ Отсутствуют права: {', '.join(missing)}</span>"
                    scopes_info += f"<br/><span style='font-size:11px; color:#666;'>Текущие права: {', '.join(scope_names) if scope_names else 'нет данных'}</span>"
                else:
                    scopes_info = f"<br/><span style='color:#4caf50;'>✓ Все необходимые права присутствуют</span>"
                    scopes_info += f"<br/><span style='font-size:11px; color:#666;'>Права: {', '.join(scope_names)}</span>"
            
            return HTMLResponse(f"<span style='color:green'>Токен валиден</span>{scopes_info}")
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>Сетевой сбой: {str(exc)}</span>")


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
                    blocks.append(f"<div><b>{label}:</b> OK {pid} {('<a href=\'" + link + "\' target=\'_blank\'>link</a>') if link else ''}<br/><pre>" + ("\n".join(logs)) + "</pre></div>")
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
                    blocks.append(f"<div><b>{label}:</b> ERROR<br/><pre>" + ("\n".join(logs)) + "</pre></div>")
            else:
                blocks.append(f"<div><b>{label}:</b> not configured</div>")
        html = "".join(blocks)
        return HTMLResponse(f"<div>{html}</div>")
    except Exception as exc:
        try:
            logger.exception(f"fb.test outer error: {exc}")
        except Exception:
            pass
        return HTMLResponse(f"<span style='color:red'>FB test failed: {str(exc)}</span>")

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
    save_settings({
        f'fb_{lang}_page_id': (page_id or '').strip(),
        f'fb_{lang}_page_access_token': (page_token or '').strip(),
    })
    logger.info(f"fb.assign_page lang={lang} page_id={page_id} name={page_name}")
    return HTMLResponse(
        f"<span style='color:green'>Saved \"{_html.escape(page_name or page_id)}\" as {lang.upper()} page ✓</span>"
    )


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
    rel_basename = os.path.basename(rel)
    # Save to draft as media_vertical_path
    save_draft({"media_vertical_path": rel_basename})
    return {"output_path": rel, "output_rel": rel_basename, "output_url": f"/media/{rel_basename}"}


@router.post("/video/render_horizontal")
def video_render_horizontal(
    video: UploadFile = File(...),
    music: UploadFile = File(None),
    resolution: str = Form("1080p"),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
    music_offset_s: float = Form(0.0),
    fade_in_s: float = Form(1.0),
    fade_out_s: float = Form(1.0),
    start_time: float = Form(0.0),
    end_time: float = Form(0.0),
    zoom: float = Form(1.0),
):
    """Render horizontal 16:9 video with optional music and effects."""
    os.makedirs('render', exist_ok=True)
    os.makedirs('media', exist_ok=True)
    # Save inputs
    base = str(int(time.time()))
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
    
    # Import the new function
    from app.services.video_editor import render_horizontal_with_music
    
    # Write final output into media so it is web-accessible
    res_suffix = resolution.replace('p', '') if resolution else '1080'
    out_path = os.path.join('media', f"{base}_horizontal_{res_suffix}p.mp4")
    render_horizontal_with_music(
        input_video=video_path,
        output_video=out_path,
        resolution=resolution,
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
    rel_basename = os.path.basename(rel)
    # Save to draft as media_normal_path (for horizontal platforms)
    save_draft({"media_normal_path": rel_basename})
    return {"output_path": rel, "output_rel": rel_basename, "output_url": f"/media/{rel_basename}"}


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


@router.get("/music/list")
def music_list():
    """List all music files in configured music directory with metadata."""
    try:
        cfg = load_settings()
        base = (cfg.get("music_dir") or "").strip()
        if not base:
            return {"ok": True, "tracks": []}
        import os
        if not os.path.isdir(base):
            return {"ok": True, "tracks": []}
        exts = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma'}
        items = []
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in exts:
                # Get duration using pydub
                try:
                    from pydub import AudioSegment
                    seg = AudioSegment.from_file(p)
                    duration_ms = len(seg)
                    duration_sec = duration_ms / 1000.0
                except Exception:
                    duration_ms = 0
                    duration_sec = 0.0
                
                items.append({
                    "name": name,
                    "path": p,
                    "duration_ms": duration_ms,
                    "duration_sec": round(duration_sec, 2),
                })
        return {"ok": True, "tracks": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/music/scan")
def music_scan():
    """Alias for music/list - scans music directory and returns track list with metadata."""
    return music_list()


@router.get("/music/waveform")
def music_waveform(path: str = Query("")):
    try:
        import os, base64
        from pydub import AudioSegment
        p = path
        if not p:
            return HTMLResponse("<span style='color:red'>missing path</span>", status_code=400)
        if not os.path.isfile(p):
            return HTMLResponse("<span style='color:red'>not a file</span>", status_code=404)
        # Load audio and compute naive RMS over buckets (fast)
        seg = AudioSegment.from_file(p)
        duration_ms = len(seg)
        buckets = 400  # number of samples for display
        step = max(1, duration_ms // buckets)
        samples = []
        for i in range(0, duration_ms, step):
            chunk = seg[i:i+step]
            samples.append(int(chunk.rms))
        # Normalize 0..1
        m = max(1, max(samples) if samples else 1)
        norm = [round(s / m, 4) for s in samples]
        return {"ok": True, "duration_ms": duration_ms, "samples": norm}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/steam/translate_text")
def steam_translate_text(
    title_ru: str = Form(""),
    subtitle: str = Form(""),
    short_desc: str = Form(""),
    body_ru: str = Form(""),
):
    """Translate Steam RU fields to EN in one go. Uses OpenAI first, then falls back to public translators."""
    src = {
        "title": (title_ru or "").strip(),
        "subtitle": (subtitle or "").strip(),
        "short_desc": (short_desc or "").strip(),
        "body": (body_ru or "").strip(),
    }
    provider = ""
    out = {"title_en": "", "subtitle_en": "", "short_desc_en": "", "body_en": ""}
    try:
        cfg = load_settings()
        api_key = (cfg.get("openai_api_key") or "").strip()
        model = (cfg.get("openai_model") or "gpt-4o-mini").strip()
        if api_key and model:
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            system = (
                "You are a professional localization translator for Steam Partner Events. "
                "Translate Russian to natural, idiomatic English. Preserve Steam markup tags (e.g., [h1], [b], [url], [list]) exactly as-is in the 'body'. "
                "Return ONLY a compact JSON object with keys: title_en, subtitle_en, short_desc_en, body_en."
            )
            user = (
                f"title: {src['title']}\n"
                f"subtitle: {src['subtitle']}\n"
                f"short_desc: {src['short_desc']}\n"
                f"body: {src['body']}\n"
                "Translate all fields to English."
            )
            payload = {"model": model, "temperature": 0.2, "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]}
            with httpx.Client(timeout=25) as client:
                r = client.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload)
                if r.status_code < 400:
                    data = r.json()
                    content = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                    import json as _json
                    try:
                        parsed = _json.loads(content)
                        out.update({
                            "title_en": (parsed.get("title_en") or "").strip(),
                            "subtitle_en": (parsed.get("subtitle_en") or "").strip(),
                            "short_desc_en": (parsed.get("short_desc_en") or "").strip(),
                            "body_en": (parsed.get("body_en") or "").strip(),
                        })
                        provider = f"openai:{model}"
                    except Exception as pe:
                        try:
                            logger.warning("steam_translate parse_json_failed raw=%s", content[:400])
                        except Exception:
                            pass
                else:
                    try:
                        logger.warning("openai_http_error steam status=%s model=%s body=%s", r.status_code, model, r.text[:400])
                    except Exception:
                        pass
    except Exception as exc:
        try:
            logger.warning("steam_translate openai_error: %s", str(exc))
        except Exception:
            pass
    # Fallbacks if any field missing
    def contains_cyrillic(s: str) -> bool:
        return any('а' <= ch <= 'я' or 'А' <= ch <= 'Я' for ch in s)
    if not all(out.values()) or any(contains_cyrillic(v) for v in out.values()):
        try:
            from deep_translator import GoogleTranslator, MyMemoryTranslator, LibreTranslator
            def tr(s: str) -> str:
                if not s: return ""
                for host in ('https://libretranslate.de','https://translate.astian.org'):
                    try:
                        v = LibreTranslator(source='ru', target='en', host=host).translate(s) or ""
                        if v and not contains_cyrillic(v):
                            return v
                    except Exception: pass
                try:
                    v = GoogleTranslator(source='ru', target='en').translate(s) or ""
                    if v and not contains_cyrillic(v):
                        return v
                except Exception: pass
                try:
                    v = MyMemoryTranslator(source='ru', target='en').translate(s) or ""
                    if v and not contains_cyrillic(v):
                        return v
                except Exception: pass
                return s
            if not out["title_en"] or contains_cyrillic(out["title_en"]): out["title_en"] = tr(src['title'])
            if not out["subtitle_en"] or contains_cyrillic(out["subtitle_en"]): out["subtitle_en"] = tr(src['subtitle'])
            if not out["short_desc_en"] or contains_cyrillic(out["short_desc_en"]): out["short_desc_en"] = tr(src['short_desc'])
            if not out["body_en"] or contains_cyrillic(out["body_en"]): out["body_en"] = tr(src['body'])
            if not provider:
                provider = "fallback"
        except Exception as exc:
            provider = provider or "fallback"
    try:
        logger.info(f"steam_translate provider={provider} tlen={len(src['title'])} blen={len(src['body'])}")
    except Exception:
        pass
    out["provider"] = provider or ""
    return out


@router.get("/jobs/active")
def jobs_active(session: Session = Depends(get_session)):
    from app.models.models import Job, JobStatusEnum
    from sqlmodel import select
    rows = session.exec(
        select(Job).where(Job.status.in_([JobStatusEnum.queued, JobStatusEnum.running])).order_by(Job.created_at.desc()).limit(100)
    ).all()
    items = []
    for j in rows:
        items.append({
            "id": j.id,
            "kind": getattr(j.kind, 'value', str(j.kind)),
            "status": getattr(j.status, 'value', str(j.status)),
            "created_at": getattr(j, 'created_at', None).isoformat() if getattr(j, 'created_at', None) else None,
        })
    return {"count": len(items), "jobs": items}


@router.post("/jobs/cleanup_stuck")
def jobs_cleanup_stuck(minutes: int = 10, action: str = "mark_error", session: Session = Depends(get_session)):
    """Mark 'running' jobs older than N minutes as error or retry them.
    action in {mark_error, retry}
    """
    from app.models.models import Job, JobStatusEnum, Post, PostStatusEnum, JobKindEnum
    from sqlmodel import select
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(minutes=max(1, minutes))
    rows = session.exec(
        select(Job).where(Job.status == JobStatusEnum.running).order_by(Job.created_at.asc()).limit(200)
    ).all()
    affected = []
    for j in rows:
        try:
            created = getattr(j, 'created_at', None)
            if not created or created > cutoff:
                continue
            # Extract post_id if present
            import json as _json
            post_id = None
            try:
                post_id = (_json.loads(j.payload_json or '{}') or {}).get('post_id')
            except Exception:
                post_id = None
            p = session.get(Post, post_id) if post_id else None
            if action == 'retry':
                # Mark job error and requeue a new job for the same post
                j.status = JobStatusEnum.error
                j.last_error = (j.last_error or '') + ' [auto-cleanup: timeout]'
                if p is not None and p.status == PostStatusEnum.running:
                    p.status = PostStatusEnum.queued
                session.add(j)
                if p is not None:
                    session.add(p)
                session.commit()
                # Create a new publish job
                if post_id:
                    nj = Job(kind=JobKindEnum.publish, payload_json=j.payload_json, status=JobStatusEnum.queued)
                    session.add(nj)
                    session.commit()
                    session.refresh(nj)
                    from app.jobs.tasks import publish_news_post
                    publish_news_post.schedule(args=(nj.id, post_id), delay=0)
                    affected.append({"old_job": j.id, "new_job": nj.id, "post_id": post_id})
            else:
                # mark_error
                j.status = JobStatusEnum.error
                j.last_error = (j.last_error or '') + ' [auto-cleanup: timeout]'
                if p is not None and p.status == PostStatusEnum.running:
                    p.status = PostStatusEnum.error
                    p.error_text = (p.error_text or '') + ' [auto-cleanup: timeout]'
                session.add(j)
                if p is not None:
                    session.add(p)
                session.commit()
                affected.append({"job": j.id, "post_id": post_id})
        except Exception as exc:
            try:
                logger.error(f"jobs_cleanup error for job={getattr(j,'id',None)}: {exc}")
            except Exception:
                pass
    return {"ok": True, "affected": affected}


@router.post("/jobs/kill")
def jobs_kill(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.models.models import Job, JobStatusEnum, Post, PostStatusEnum
    import json as _json
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    # mark job error and related post canceled
    job.status = JobStatusEnum.error
    job.last_error = (job.last_error or '') + ' [killed_by_user]'
    post_id = None
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if post_id:
        post = session.get(Post, post_id)
        if post and post.status in (PostStatusEnum.queued, PostStatusEnum.running):
            post.status = PostStatusEnum.canceled
            session.add(post)
    session.add(job)
    session.commit()
    # return refreshed jobs table
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    # Enrich jobs for previews/platform/thumbs
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


@router.post("/jobs/remove")
def jobs_remove(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.models.models import Job, Post, PostStatusEnum
    import json as _json
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    # Best-effort cancel related post if running/queued
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if post_id:
        post = session.get(Post, post_id)
        if post and post.status in (PostStatusEnum.queued, PostStatusEnum.running):
            post.status = PostStatusEnum.canceled
            session.add(post)
    session.delete(job)
    session.commit()
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


@router.post("/jobs/refresh_metrics")
def jobs_refresh_metrics(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.models.models import Job
    import json as _json
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if post_id:
        from app.jobs.tasks import refresh_metrics_for_post
        refresh_metrics_for_post.schedule(args=(post_id,), delay=0)
    # return refreshed table (status may update shortly after)
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


@router.post("/jobs/retry")
def jobs_retry(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    from app.models.models import Job, JobStatusEnum, JobKindEnum, Post
    import json as _json
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    # Only retry publish jobs
    if getattr(job, 'kind', None) != JobKindEnum.publish:
        return HTMLResponse("<span style='color:orange'>retry_only_for_publish</span>")
    # Extract post_id
    post_id = None
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if not post_id:
        return HTMLResponse("<span style='color:red'>no_post_in_payload</span>", status_code=400)
    # Reset the post back to queued, otherwise publish_news_post exits with
    # 'skipped_already_handled' and the new job hangs in queued forever.
    from app.models.models import PostStatusEnum as _PSE
    target_post = session.get(Post, post_id)
    do_schedule = True
    if target_post is None:
        do_schedule = False
    elif target_post.status == _PSE.done:
        # Already published — don't duplicate the post on an accidental retry
        do_schedule = False
    else:
        if target_post.status not in (_PSE.queued, _PSE.running):
            target_post.status = _PSE.queued
        target_post.error_text = None
        # Manual retry means "publish now", drop a stale schedule
        target_post.scheduled_at = None
        session.add(target_post)
        session.commit()
    if do_schedule:
        # Create a new queued job with same payload
        nj = Job(kind=JobKindEnum.publish, payload_json=job.payload_json, status=JobStatusEnum.queued)
        session.add(nj)
        session.commit()
        session.refresh(nj)
        from app.jobs.tasks import publish_news_post
        publish_news_post.schedule(args=(nj.id, post_id), delay=0)
        # The old failed job row is no longer actionable — mark it handled
        try:
            if job.status in (JobStatusEnum.queued, JobStatusEnum.error):
                job.status = JobStatusEnum.done
                job.last_error = ((job.last_error or '') + ' retried_as_job_' + str(nj.id)).strip()
                session.add(job)
                session.commit()
        except Exception:
            pass
    # refresh table
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


# ==================== Calendar API ====================

@router.get("/calendar/events")
def calendar_events(start: Optional[str] = None, end: Optional[str] = None, session: Session = Depends(get_session)):
    """Get grouped posts as FullCalendar events within the requested date range."""
    from sqlmodel import select
    from sqlmodel.sql.expression import or_
    from datetime import datetime as _dt
    from app.models.models import Post as _Post, Brief as _Brief
    
    query = select(_Post)
    if start and end:
        try:
            start_dt = _dt.fromisoformat(start.replace('Z', '').split('+')[0].split('.')[0])
            end_dt = _dt.fromisoformat(end.replace('Z', '').split('+')[0].split('.')[0])
            query = query.where(or_(
                _Post.scheduled_at.between(start_dt, end_dt),
                _Post.published_at.between(start_dt, end_dt),
                _Post.created_at.between(start_dt, end_dt),
            ))
        except Exception:
            pass
    rows = session.exec(query.order_by(_Post.created_at.desc()).limit(500)).all()
    
    groups = {}
    for p in rows:
        key = p.brief_id or f"post_{p.id}"
        if key not in groups:
            groups[key] = {'posts': [], 'brief': None}
        groups[key]['posts'].append(p)
    
    events = []
    for key, bucket in groups.items():
        posts = bucket['posts']
        brief_id = posts[0].brief_id
        brief = session.get(_Brief, brief_id) if brief_id else None
        
        preview = ''
        thumb_url = None
        brief_type = ''
        if brief:
            base = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or '')
            preview = base[:120] + ('...' if len(base) > 120 else '')
            media_rel = getattr(brief, 'media_path', None)
            if media_rel:
                name = str(media_rel).replace('\\', '/').split('/')[-1]
                thumb_url = f"/media/{name}"
            brief_type = getattr(brief, 'type', '') or ''
        
        platform_items = []
        statuses = []
        dates = []
        for p in posts:
            platform = p.platform.value if hasattr(p.platform, 'value') else str(p.platform)
            status = p.status.value if hasattr(p.status, 'value') else str(p.status)
            statuses.append(status)
            event_date = p.scheduled_at or p.published_at or p.created_at
            if event_date:
                dates.append(event_date)
            platform_items.append({
                'platform': platform,
                'status': status,
                'post_id': p.id,
                'platform_url': p.platform_url,
            })
        
        # Group status: error > running > queued > done
        if any(s == 'error' for s in statuses):
            group_status = 'error'
        elif any(s == 'running' for s in statuses):
            group_status = 'running'
        elif any(s == 'queued' for s in statuses):
            group_status = 'queued'
        elif all(s == 'done' for s in statuses):
            group_status = 'done'
        else:
            group_status = 'queued'
        
        # Event date: earliest meaningful date in group
        event_date = min(dates) if dates else None
        if not event_date:
            continue
        
        type_label = 'Post'
        if str(brief_type) == 'video':
            type_label = 'Video'
        elif str(brief_type) == 'news':
            type_label = 'Post'
        
        platforms_label = ','.join(sorted({x['platform'] for x in platform_items}))
        title = f"{type_label} ({len(platform_items)}): {preview[:40]}{'...' if len(preview) > 40 else ''}"
        
        events.append({
            'id': f"brief_{brief_id or key}",
            'title': title,
            'start': event_date.isoformat(),
            'allDay': False,
            'startEditable': group_status == 'queued',
            'extendedProps': {
                'brief_id': brief_id,
                'status': group_status,
                'preview': preview,
                'thumb_url': thumb_url,
                'platforms': platform_items,
                'platforms_label': platforms_label,
                'type_label': type_label,
                'post_ids': [x['post_id'] for x in platform_items],
            }
        })
    
    return {'events': events}


class CalendarMoveRequest(BaseModel):
    post_id: Optional[int] = None
    brief_id: Optional[int] = None
    post_ids: Optional[List[int]] = None
    new_date: str


@router.post("/calendar/move")
def calendar_move(req: CalendarMoveRequest, session: Session = Depends(get_session)):
    """Move a group (brief) or single post to a new date (drag-drop from calendar)."""
    from datetime import datetime as _dt
    from app.models.models import Post, PostStatusEnum
    from sqlmodel import select
    
    # Parse new date
    try:
        date_str = req.new_date.replace('Z', '').split('+')[0].split('.')[0]
        new_dt = _dt.fromisoformat(date_str)
    except Exception as e:
        return {'ok': False, 'error': f'Invalid date format: {e}'}
    
    # Update group if brief_id is provided
    if req.brief_id:
        posts = session.exec(
            select(Post).where(Post.brief_id == req.brief_id, Post.status == PostStatusEnum.queued)
        ).all()
        if not posts:
            return {'ok': False, 'error': 'No queued posts in this group'}
        for p in posts:
            p.scheduled_at = new_dt
            session.add(p)
        session.commit()
        return {'ok': True}
    
    # Update list of post_ids if provided
    if req.post_ids:
        posts = session.exec(
            select(Post).where(Post.id.in_(req.post_ids), Post.status == PostStatusEnum.queued)
        ).all()
        if not posts:
            return {'ok': False, 'error': 'No queued posts found'}
        for p in posts:
            p.scheduled_at = new_dt
            session.add(p)
        session.commit()
        return {'ok': True}
    
    # Fallback to single post_id
    if not req.post_id:
        return {'ok': False, 'error': 'Missing post_id'}
    post = session.get(Post, req.post_id)
    if not post:
        return {'ok': False, 'error': 'Post not found'}
    if post.status != PostStatusEnum.queued:
        return {'ok': False, 'error': 'Can only move queued posts'}
    
    # Update post
    post.scheduled_at = new_dt
    session.add(post)
    session.commit()
    
    return {'ok': True}


# ==================== Settings Status & Post Management ====================

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


@router.post("/posts/{post_id}/publish_now")
def publish_post_now(post_id: int, session: Session = Depends(get_session)):
    """Immediately publish a scheduled post."""
    from app.models.models import Post, PostStatusEnum, Job, JobKindEnum, JobStatusEnum
    import json as _json
    
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    if post.status != PostStatusEnum.queued:
        raise HTTPException(status_code=400, detail="Post is not in queued status")
    
    post.scheduled_at = None
    session.add(post)
    session.commit()
    
    payload = {"post_id": post.id}
    job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
    session.add(job)
    session.commit()
    session.refresh(job)
    
    from app.jobs.tasks import publish_news_post
    publish_news_post.schedule(args=(job.id, post.id), delay=0)
    
    return {"ok": True, "message": "Publishing now", "job_id": job.id}


@router.get("/posts/{post_id}/edit_data")
def get_post_edit_data(post_id: int, session: Session = Depends(get_session)):
    """Get post and brief data for editing."""
    from app.models.models import Post, Brief
    
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    brief = session.get(Brief, post.brief_id) if post.brief_id else None
    
    return {
        'post': {
            'id': post.id,
            'platform': post.platform.value if hasattr(post.platform, 'value') else str(post.platform),
            'status': post.status.value if hasattr(post.status, 'value') else str(post.status),
            'scheduled_at': post.scheduled_at.isoformat() if post.scheduled_at else None,
            'brief_id': post.brief_id,
        },
        'brief': {
            'id': brief.id if brief else None,
            'text_ru': brief.text_ru if brief else '',
            'text_en': brief.text_en if brief else '',
            'title': brief.title if brief else '',
            'tags_json': brief.tags_json if brief else '',
            'media_path': brief.media_path if brief else None,
        } if brief else None
    }


@router.post("/posts/{post_id}/update")
def update_post(
    post_id: int,
    text_ru: Optional[str] = Form(None),
    text_en: Optional[str] = Form(None),
    title: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    session: Session = Depends(get_session)
):
    """Update post and its brief. Updates all queued posts sharing the same brief."""
    from app.models.models import Post, Brief, PostStatusEnum
    from datetime import datetime as _dt
    
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    if post.brief_id:
        brief = session.get(Brief, post.brief_id)
        if brief:
            if text_ru is not None:
                brief.text_ru = text_ru
            if text_en is not None:
                brief.text_en = text_en
            if title is not None:
                brief.title = title
            if tags is not None:
                brief.tags_json = tags
            session.add(brief)
    
    if scheduled_at is not None:
        new_dt = None
        if scheduled_at.strip():
            try:
                new_dt = _dt.fromisoformat(scheduled_at.strip())
            except Exception:
                pass
        
        post.scheduled_at = new_dt
        session.add(post)
        
        if post.brief_id:
            from sqlmodel import select
            related = session.exec(
                select(Post).where(
                    Post.brief_id == post.brief_id,
                    Post.status == PostStatusEnum.queued,
                    Post.id != post.id
                )
            ).all()
            for p in related:
                p.scheduled_at = new_dt
                session.add(p)
    
    session.commit()
    return {"ok": True, "message": "Post updated"}


@router.post("/posts/{post_id}/delete_remote")
def delete_post_remote(post_id: int, session: Session = Depends(get_session)):
    """Delete a post from the remote platform. Returns fallback URL if API delete not supported."""
    from app.models.models import Post
    
    post = session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    
    cfg = load_settings()
    platform = str(post.platform)
    deleted = False
    error = None
    fallback_url = post.platform_url
    
    try:
        if platform.endswith('vk') and post.platform_post_id:
            token = (cfg.get('vk_access_token') or '').strip()
            if token and post.platform_post_id:
                parts = post.platform_post_id.split('_')
                owner_id = parts[0] if len(parts) > 0 else ''
                post_vk_id = parts[1] if len(parts) > 1 else ''
                if owner_id and post_vk_id:
                    with httpx.Client(timeout=15) as client:
                        r = client.post("https://api.vk.com/method/wall.delete", data={
                            "owner_id": owner_id,
                            "post_id": post_vk_id,
                            "access_token": token,
                            "v": "5.199"
                        })
                        d = r.json()
                        if d.get('response') == 1:
                            deleted = True
                        else:
                            error = str(d.get('error', d))
        
        elif platform.endswith('x') and post.platform_post_id:
            try:
                token = _x_ensure_token()
                with httpx.Client(timeout=15) as client:
                    r = client.delete(
                        f"https://api.twitter.com/2/tweets/{post.platform_post_id}",
                        headers={"Authorization": f"Bearer {token}"}
                    )
                    if r.status_code < 300:
                        deleted = True
                    else:
                        error = f"HTTP {r.status_code}: {r.text[:200]}"
            except Exception as e:
                error = str(e)
        
        elif platform in ('fb_ru', 'fb_en', 'PlatformEnum.fb_ru', 'PlatformEnum.fb_en') and post.platform_post_id:
            is_ru = 'ru' in platform
            page_token = (cfg.get('fb_ru_page_access_token') if is_ru else cfg.get('fb_en_page_access_token')) or ''
            if page_token:
                with httpx.Client(timeout=15) as client:
                    r = client.delete(
                        f"https://graph.facebook.com/v19.0/{post.platform_post_id}",
                        params={"access_token": page_token}
                    )
                    if r.status_code < 300:
                        deleted = True
                    else:
                        error = f"HTTP {r.status_code}: {r.text[:200]}"
        
        elif platform.endswith('reddit') and post.platform_post_id:
            try:
                from app.services.reddit_client import RedditClient
                cid = (cfg.get('reddit_client_id') or '').strip()
                csec = (cfg.get('reddit_client_secret') or '').strip()
                uname = (cfg.get('reddit_username') or '').strip()
                pwd = (cfg.get('reddit_password') or '').strip()
                if cid and csec and uname and pwd:
                    rc = RedditClient(cid, csec, uname, pwd)
                    sid = post.platform_post_id.replace('t3_', '')
                    submission = rc.reddit.submission(id=sid)
                    submission.delete()
                    deleted = True
            except Exception as e:
                error = str(e)
        
        elif platform.endswith('tg') and post.platform_post_id:
            token = (cfg.get('tg_bot_token') or '').strip()
            chat_id = (cfg.get('tg_chat_id_ru') or '').strip()
            if token and chat_id and post.platform_post_id:
                try:
                    from app.services.telegram_client import TelegramClient
                    tg = TelegramClient(token)
                    result = tg.delete_message(chat_id, int(post.platform_post_id))
                    if result.get('ok'):
                        deleted = True
                    else:
                        error = str(result)
                except Exception as e:
                    error = str(e)
        
        else:
            error = "Platform does not support API deletion"
    
    except Exception as e:
        error = str(e)
    
    return {
        "deleted": deleted,
        "error": error,
        "fallback_url": fallback_url if not deleted else None,
        "platform": platform
    }


# ── Projects ─────────────────────────────────────────────────────────────────

def _projects_list_html(request: Request, session: Session) -> HTMLResponse:
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    projects = [
        {
            "id": p.id,
            "name": p.name,
            "icon_url": f"/media/{p.icon_path}" if p.icon_path else None,
            "tags_json": p.tags_json or "",
            "platforms_json": p.platforms_json or "{}",
            "sort_order": p.sort_order,
        }
        for p in rows
    ]
    return templates_api.TemplateResponse(
        "partials/projects_list.html",
        {"request": request, "projects": projects},
    )


@router.get("/projects")
def list_projects(session: Session = Depends(get_session)):
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    return [
        {
            "id": p.id,
            "name": p.name,
            "icon_url": f"/media/{p.icon_path}" if p.icon_path else None,
            "tags_json": p.tags_json or "",
            "platforms_json": p.platforms_json or "{}",
            "sort_order": p.sort_order,
        }
        for p in rows
    ]


@router.get("/projects/partial", response_class=HTMLResponse)
def projects_partial(request: Request, session: Session = Depends(get_session)):
    return _projects_list_html(request, session)


@router.post("/projects", response_class=HTMLResponse)
def create_project(
    request: Request,
    name: str = Form(...),
    tags_json: Optional[str] = Form(""),
    platforms_json: Optional[str] = Form("{}"),
    sort_order: Optional[int] = Form(0),
    icon: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
):
    icon_path = None
    if icon and icon.filename:
        os.makedirs("media", exist_ok=True)
        safe_name = icon.filename.replace("..", "").replace("/", "_").replace("\\", "_")
        rel = f"media/{int(time.time())}_{safe_name}"
        with open(rel, "wb") as f:
            f.write(icon.file.read())
        icon_path = rel.split("media/", 1)[-1]
    p = Project(
        user_id=1,
        name=(name or "").strip(),
        icon_path=icon_path,
        tags_json=(tags_json or "").strip(),
        platforms_json=(platforms_json or "{}").strip(),
        sort_order=sort_order or 0,
    )
    session.add(p)
    session.commit()
    return _projects_list_html(request, session)


@router.post("/projects/{project_id}/update", response_class=HTMLResponse)
def update_project(
    project_id: int,
    request: Request,
    name: Optional[str] = Form(None),
    tags_json: Optional[str] = Form(None),
    platforms_json: Optional[str] = Form(None),
    sort_order: Optional[int] = Form(None),
    icon: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if name is not None:
        p.name = name.strip()
    if tags_json is not None:
        p.tags_json = tags_json.strip()
    if platforms_json is not None:
        p.platforms_json = platforms_json.strip()
    if sort_order is not None:
        p.sort_order = sort_order
    if icon and icon.filename:
        os.makedirs("media", exist_ok=True)
        safe_name = icon.filename.replace("..", "").replace("/", "_").replace("\\", "_")
        rel = f"media/{int(time.time())}_{safe_name}"
        with open(rel, "wb") as f:
            f.write(icon.file.read())
        p.icon_path = rel.split("media/", 1)[-1]
    p.updated_at = now_ts()
    session.add(p)
    session.commit()
    return _projects_list_html(request, session)


@router.post("/projects/{project_id}/delete", response_class=HTMLResponse)
def delete_project(
    project_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    session.delete(p)
    session.commit()
    return _projects_list_html(request, session)