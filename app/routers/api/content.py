"""Post authoring: translation, hashtags, drafts, draft media, briefs."""

import os
import time
from app.db.session import get_session
from app.models.models import Brief
from app.services.draft_store import load_draft
from app.services.draft_store import save_draft
from app.services import openai_client
from fastapi import APIRouter
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
from loguru import logger
from pydantic import BaseModel
from sqlmodel import Session
from typing import List
from typing import Optional
from app.routers.api.common import _e

router = APIRouter()

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
def test_openai(openai_model: Optional[str] = None):
    """Settings → OpenAI → Test; tries the model typed in the form (before saving)."""
    try:
        api_key, _ = openai_client.settings()
        if not api_key:
            return {"ok": False, "error": "no_api_key"}
        r, model = openai_client.chat_completion([
            {"role": "system", "content": "You translate RU to EN."},
            {"role": "user", "content": "Переведи на английский: Привет мир"},
        ], temperature=0, model=(openai_model or "").strip() or None, timeout=30)
        data = {"status": r.status_code, "model": model}
        if r.status_code < 400:
            data["translation"] = openai_client.content_of(r)
        else:
            try:
                data["json"] = r.json()
            except Exception:
                data["text"] = r.text[:400]
        return {"ok": r.status_code < 400, "response": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/translate/test_claude")
def test_claude(anthropic_model: Optional[str] = None):
    """Settings → AI → Claude → Test; tries the model typed in the form (before saving)."""
    from app.services import anthropic_client
    try:
        text, model = anthropic_client.chat([
            {"role": "system", "content": "You translate RU to EN. Answer with the translation only."},
            {"role": "user", "content": "Привет мир"},
        ], max_tokens=2000, model=(anthropic_model or "").strip() or None, timeout=60)
        return {"ok": bool(text), "response": {"model": model, "translation": text}}
    except anthropic_client.ClaudeError as exc:
        return {"ok": False, "error": str(exc)}


@router.get("/translate/test_deepl")
def test_deepl():
    """Settings → AI → DeepL → Test."""
    from app.services import machine_translate
    try:
        return {"ok": True, "response": {"translation": machine_translate.deepl("Привет мир", "en", "ru")}}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/translate")
def translate(ru_text: Optional[str] = Form(None), text_ru: Optional[str] = Form(None)):
    """RU → EN for the post forms: the AI from Settings (OpenAI or Claude), then machine translation (DeepL or the
    free translators), then the source marked "EN:" so the post still goes out."""
    from app.services import llm, machine_translate
    src = (ru_text or text_ru or "").strip()
    if not src:
        return HTMLResponse("<textarea id=\"text_en\" name=\"text_en\" rows=\"6\" cols=\"80\"></textarea>")
    en, provider = "", ""
    try:
        en, provider = llm.chat([
            {"role": "system", "content": "You are a professional localization translator. Translate Russian to natural, idiomatic English. Preserve Steam markup tags (e.g., [h1], [b], [url], [list]) exactly as-is. Do not add commentary; return only the translated text."},
            {"role": "user", "content": "Translate to English, preserving markup:\n\n" + src},
        ], temperature=0.2, timeout=60)
    except llm.LLMError as exc:
        logger.warning(f"translate ai: {exc}")
    if not machine_translate.looks_translated(src, en, "en"):
        en, provider = machine_translate.translate(src, "en", "ru")
    if not en:
        en, provider = f"EN: {src}", "fallback"
    save_draft({"text_ru": src, "text_en": en})
    logger.info(f"translate provider={provider} in_len={len(src)} out_len={len(en)}")
    html = f"<textarea id=\"text_en\" name=\"text_en\" rows=\"6\" cols=\"80\" data-translator=\"{_e(provider)}\">{_e(en)}</textarea><!-- provider: {_e(provider)} -->"
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
        f"<input id=\"tags_input\" type=\"text\" name=\"tags\" value=\"{_e(tag_str)}\" style=\"width: 520px;\" />"
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
    return HTMLResponse("<span style='color:green'>Draft saved</span>")


@router.post("/draft/clear")
def clear_news_draft():
    from app.services.draft_store import clear_draft
    clear_draft()
    return HTMLResponse("<span style='color:green'>Draft cleared</span>")


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
    return HTMLResponse("<span style='color:green'>Gallery updated</span>")


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
