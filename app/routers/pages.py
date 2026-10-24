import os
from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from app.core.templating import make_templates
from sqlmodel import Session

from app.db.session import get_session
from app.models.models import Brief, Post, PlatformEnum, PostStatusEnum, Job, JobKindEnum, JobStatusEnum, Project
from app.jobs.tasks import publish_news_post
from app.services.draft_store import load_draft, save_draft, clear_draft
from app.services import music_library
from app.services.settings import load_settings
from app.jobs.tasks import refresh_metrics_for_post
from app.routers.api.common import form_texts

router = APIRouter()
templates = make_templates()

def _enrich_posts(posts):
    import json as _json
    from sqlmodel import Session as _Session
    from app.db.session import engine as _engine
    from app.models.models import Brief as _Brief
    from app.services import thumbs
    enriched = []
    briefs = {}
    _s = _Session(_engine)
    for p in posts:
        metrics = {}
        if getattr(p, 'metrics_json', None):
            try:
                metrics = _json.loads(p.metrics_json) or {}
            except Exception:
                metrics = {}
        # Build preview and tags from brief
        preview = ''
        tags_val = None
        thumb_url = None
        thumb_is_video = False
        is_short = False
        try:
            bid = getattr(p, 'brief_id', None)
            if bid not in briefs:
                briefs[bid] = _s.get(_Brief, bid)
            b = briefs[bid]
            if b is not None:
                base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                preview = (base[:140] + ('…' if len(base) > 140 else ''))
                tags_val = getattr(b, 'tags_json', None)
                # a rendered Shorts-Factory clip can be reposted to more networks
                is_short = bool(getattr(b, 'vertical_916', None) and getattr(b, 'media_vertical_path', None))
                # a video can't be an <img>: its poster frame stands in
                thumb_url = thumbs.thumb_url(b)
                thumb_is_video = thumbs.is_video(getattr(b, 'media_path', None))
        except Exception:
            preview = ''
        platform_text = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
        platform_url = getattr(p, 'platform_url', None)
        # Fix Reddit links: prefer the post permalink over media URLs
        try:
            if platform_text == 'reddit' and getattr(p, 'platform_post_id', None):
                if not platform_url or any(x in (platform_url or '') for x in ['redd.it', 'i.redd.it', 'preview.redd.it']):
                    platform_url = f"https://reddit.com/comments/{p.platform_post_id}"
        except Exception:
            platform_url = getattr(p, 'platform_url', None)
        from app.services.publications import engagement
        e = engagement(metrics)
        m_like, m_repost, m_reply, m_quote, m_views = e['likes'], e['reposts'], e['comments'], e['quotes'], e['views']
        # Total engagement: any direct reaction (views excluded — they are reach, not reaction)
        m_total = e['reactions'] or 0
        has_metrics = e['has']
        row = {
            'id': p.id,
            'platform_text': platform_text,
            # two channels of one network differ only by this (Settings → Languages & destinations)
            'destination': getattr(p, 'destination_id', None),
            'status_text': (p.status.value if hasattr(p.status, 'value') else str(p.status)),
            'brief_id': getattr(p, 'brief_id', None),
            'scheduled_at': getattr(p, 'scheduled_at', None),
            'published_at': getattr(p, 'published_at', None),
            'platform_url': platform_url,
            'm_like': m_like,
            'm_repost': m_repost,
            'm_reply': m_reply,
            'm_quote': m_quote,
            'm_views': m_views,
            'm_total': m_total,
            'has_metrics': has_metrics,
            'tags': tags_val,
            'preview': preview,
            'thumb_url': thumb_url,
            'thumb_is_video': thumb_is_video,
            'is_short': is_short,
            'm_error': metrics.get('error'),
            'error_text': getattr(p, 'error_text', None),
        }
        enriched.append(row)
    _s.close()
    return enriched

@router.get('/news', response_class=HTMLResponse)
def news_form(request: Request):
    draft = load_draft()
    cfg = load_settings()
    platforms_ready = {
        'discord': bool((cfg.get('discord_webhook_ru') or cfg.get('discord_webhook_en') or cfg.get('discord_webhook_url') or '').strip()),
        'x': bool((cfg.get('x_access_token') or '').strip()),
        'vk': bool(((cfg.get('vk_access_token') or '').strip()) and ((cfg.get('vk_group_id') or '').strip())),
        'yt': bool(((cfg.get('yt_access_token') or '').strip()) and ((cfg.get('yt_refresh_token') or '').strip())),
        'fb_ru': bool(((cfg.get('fb_ru_page_id') or '').strip()) and ((cfg.get('fb_ru_page_access_token') or '').strip())),
        'fb_en': bool(((cfg.get('fb_en_page_id') or '').strip()) and ((cfg.get('fb_en_page_access_token') or '').strip())),
        'reddit': bool(((cfg.get('reddit_client_id') or '').strip()) and ((cfg.get('reddit_client_secret') or '').strip()) and ((cfg.get('reddit_username') or '').strip()) and ((cfg.get('reddit_password') or '').strip()) and ((cfg.get('reddit_subreddit_en') or '').strip())),
        'tg': bool(((cfg.get('tg_bot_token') or '').strip()) and ((cfg.get('tg_chat_id_ru') or '').strip())),
        'bluesky': bool(((cfg.get('bluesky_handle') or '').strip()) and ((cfg.get('bluesky_app_password') or '').strip())),
        'tiktok': True,
    }
    return templates.TemplateResponse('news.html', {'request': request, 'draft': draft, 'platforms_ready': platforms_ready, 'ui_language': cfg.get('ui_language', 'en')})

@router.get('/steam', response_class=HTMLResponse)
def steam_export_page(request: Request):
    draft = load_draft()
    cfg = load_settings()
    return templates.TemplateResponse('steam_export.html', {'request': request, 'draft': draft, 'ui_language': cfg.get('ui_language', 'en')})

@router.get('/projects', response_class=HTMLResponse)
def projects_page(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    from app.routers.api.projects import _project_dict
    from app.services.marketing import PROJECT_STAGES, PROJECT_STATUSES
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    cfg = load_settings()
    return templates.TemplateResponse('projects.html', {
        'request': request,
        'projects': [_project_dict(p) for p in rows],
        'stages': PROJECT_STAGES,
        'statuses': PROJECT_STATUSES,
        'ui_language': cfg.get('ui_language', 'en'),
    })


@router.get('/accounts', response_class=HTMLResponse)
def accounts(request: Request):
    from app.services import destinations
    cfg = load_settings()
    token = cfg.get('vk_access_token', '') or ''
    token_hint = ('•••' + token[-4:]) if token else ''
    return templates.TemplateResponse(
        'accounts.html',
        {
            'request': request,
            'ui_language': cfg.get('ui_language', 'en'),
            'dest_editor': destinations.editor_context(cfg),
            'discord_webhook': cfg.get('discord_webhook_url', ''),
            'discord_bot_token_hint': (('•••' + (cfg.get('discord_bot_token') or '')[-4:]) if (cfg.get('discord_bot_token') or '') else ''),
            'discord_guild_id': cfg.get('discord_guild_id', ''),
            'steam_key_hint': (('•••' + (cfg.get('steam_financial_api_key') or '')[-4:]) if (cfg.get('steam_financial_api_key') or '') else ''),
            'vk_client_id': cfg.get('vk_client_id', ''),
            'vk_client_secret': cfg.get('vk_client_secret', ''),
            'vk_group_id': cfg.get('vk_group_id', ''),
            'vk_redirect_uri': cfg.get('vk_redirect_uri', ''),
            'vk_token_hint': token_hint,
            'vk_user_token_hint': (('•••' + (cfg.get('vk_user_access_token') or '')[-4:]) if (cfg.get('vk_user_access_token') or '') else ''),
            'x_client_id': cfg.get('x_client_id', ''),
            'x_client_secret': cfg.get('x_client_secret', ''),
            'x_redirect_uri': cfg.get('x_redirect_uri', ''),
            'x_api_key': cfg.get('x_api_key', ''),
            'x_api_secret': cfg.get('x_api_secret', ''),
            'x_oauth_token': cfg.get('x_oauth_token', ''),
            'x_oauth_token_secret': cfg.get('x_oauth_token_secret', ''),
            'yt_client_id': cfg.get('yt_client_id', ''),
            'yt_client_secret': cfg.get('yt_client_secret', ''),
            'yt_access_token': cfg.get('yt_access_token', ''),
            'yt_refresh_token': cfg.get('yt_refresh_token', ''),
            'yt_metrics_days': cfg.get('yt_metrics_days', 90),
            'fb_app_id': cfg.get('fb_app_id', ''),
            'fb_app_secret': cfg.get('fb_app_secret', ''),
            'fb_ru_page_id': cfg.get('fb_ru_page_id', ''),
            'fb_en_page_id': cfg.get('fb_en_page_id', ''),
            'fb_ru_page_access_token': cfg.get('fb_ru_page_access_token', ''),
            'fb_en_page_access_token': cfg.get('fb_en_page_access_token', ''),
            'ig_user_id': cfg.get('ig_user_id', ''),
            'ig_access_token_hint': (('•••' + (cfg.get('ig_access_token') or '')[-4:]) if (cfg.get('ig_access_token') or '') else ''),
            'bluesky_handle': cfg.get('bluesky_handle', ''),
            'bluesky_service': cfg.get('bluesky_service', 'https://bsky.social'),
            'bluesky_pw_hint': (('•••' + (cfg.get('bluesky_app_password') or '')[-4:]) if (cfg.get('bluesky_app_password') or '') else ''),
            'reddit_client_id': cfg.get('reddit_client_id', ''),
            'reddit_client_secret': cfg.get('reddit_client_secret', ''),
            'reddit_username': cfg.get('reddit_username', ''),
            'reddit_subreddit_en': cfg.get('reddit_subreddit_en', ''),
            'openai_api_key': cfg.get('openai_api_key', ''),
            'openai_model': cfg.get('openai_model', ''),
            'llm_provider': cfg.get('llm_provider') or 'auto',
            'openai_key_hint': (('•••' + (cfg.get('openai_api_key') or '')[-4:]) if (cfg.get('openai_api_key') or '') else ''),
            'anthropic_model': cfg.get('anthropic_model', ''),
            'anthropic_key_hint': (('•••' + (cfg.get('anthropic_api_key') or '')[-4:]) if (cfg.get('anthropic_api_key') or '') else ''),
            'deepl_key_hint': (('•••' + (cfg.get('deepl_api_key') or '')[-4:]) if (cfg.get('deepl_api_key') or '') else ''),
            'tiktok_client_key': cfg.get('tiktok_client_key', ''),
            'tiktok_secret_hint': (('•••' + (cfg.get('tiktok_client_secret') or '')[-4:]) if (cfg.get('tiktok_client_secret') or '') else ''),
            'tiktok_redirect_uri': cfg.get('tiktok_redirect_uri'),
            'tiktok_token_hint': (('•••' + (cfg.get('tiktok_access_token') or '')[-4:]) if (cfg.get('tiktok_access_token') or '') else ''),
            'music_dir': '\n'.join(music_library.folders(cfg)),
            'music_folders': _music_folder_rows(cfg),
            'discord_upload_limit_mb': cfg.get('discord_upload_limit_mb') or 10,
        },
    )


def _music_folder_rows(cfg) -> list:
    rows = []
    for f in music_library.folders(cfg):
        ok = os.path.isdir(f)
        n = 0
        if ok:
            for _root, _dirs, files in os.walk(f):
                n += sum(1 for x in files if os.path.splitext(x)[1].lower() in music_library.AUDIO_EXTS)
        rows.append({'path': f, 'exists': ok, 'tracks': n})
    return rows

@router.get('/jobs', response_class=HTMLResponse)
def jobs_list(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    rows = session.exec(select(Job).order_by(Job.created_at.desc()).limit(50)).all()
    # Enrich backend: flatten job fields and attach platform/preview/thumb
    from app.models.models import Post as _Post, Brief as _Brief
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
    cfg = load_settings()
    return templates.TemplateResponse('jobs.html', {'request': request, 'jobs': enriched, 'ui_language': cfg.get('ui_language', 'en')})

@router.get('/jobs/partial', response_class=HTMLResponse)
def jobs_table_partial(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    rows = session.exec(select(Job).order_by(Job.created_at.desc()).limit(50)).all()
    from app.models.models import Post as _Post, Brief as _Brief
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
                    plat = (p.platform.value if hasattr(p, 'platform') and hasattr(p.platform, 'value') else str(p.platform))
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
    cfg = load_settings()
    return templates.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched, 'ui_language': cfg.get('ui_language', 'en')})

@router.get('/posts', response_class=HTMLResponse)
def posts_list(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    rows = session.exec(select(Post).order_by(Post.created_at.desc()).limit(50)).all()
    data = _enrich_posts(rows)
    cfg = load_settings()
    return templates.TemplateResponse('posts.html', {'request': request, 'posts': data, 'ui_language': cfg.get('ui_language', 'en')})

@router.get('/analytics', response_class=HTMLResponse)
def analytics_list(request: Request):
    # the table itself loads from /api/analytics/table
    cfg = load_settings()
    return templates.TemplateResponse('analytics.html', {'request': request, 'body_class': 'page-analytics', 'ui_language': cfg.get('ui_language', 'en')})


@router.get('/status', response_class=HTMLResponse)
def health_page(request: Request):
    cfg = load_settings()
    return templates.TemplateResponse('health.html', {'request': request, 'body_class': 'page-health', 'ui_language': cfg.get('ui_language', 'en')})


@router.get('/calendar', response_class=HTMLResponse)
def calendar_view(request: Request):
    cfg = load_settings()
    return templates.TemplateResponse('calendar.html', {'request': request, 'ui_language': cfg.get('ui_language', 'en')})

@router.get('/posts/partial', response_class=HTMLResponse)
def posts_table_partial(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    rows = session.exec(select(Post).order_by(Post.created_at.desc()).limit(50)).all()
    data = _enrich_posts(rows)
    cfg = load_settings()
    return templates.TemplateResponse('partials/posts_table.html', {'request': request, 'posts': data, 'ui_language': cfg.get('ui_language', 'en')})


# ==================== Publication result (Post/Redirect/Get) ====================
# The publish forms answer with a redirect to a GET page: a reload of the result must
# never re-submit the form (a restart-triggered reload re-published a post on 2026-09-30).

def _published_redirect(brief_id: int, skipped: Optional[List[str]] = None, duplicate: bool = False):
    from urllib.parse import urlencode
    from fastapi.responses import RedirectResponse
    query = {k: v for k, v in (('skipped', ','.join(skipped or [])), ('duplicate', '1' if duplicate else '')) if v}
    return RedirectResponse(f"/published/{brief_id}" + (f"?{urlencode(query)}" if query else ''), status_code=303)


def _duplicate_redirect(session: Session, brief: Brief, platforms: Optional[List[str]], allow_duplicate: Optional[str],
                        targets: Optional[list] = None):
    """Refuse to create a publication the chosen networks already got (or are about to
    get) unless "publish again" was ticked; None = go ahead. `targets`: the chosen
    destinations ([(destination, PlatformEnum)]) when the form sent destinations."""
    if allow_duplicate:
        return None
    from app.services import duplicates
    chosen = []
    by_dest = None
    if targets is not None:
        chosen = [plat for _, plat in targets]
        by_dest = {}
        for dest, plat in targets:
            by_dest.setdefault(plat.value, []).append(dest['id'])
    else:
        for p in platforms or []:
            try:
                chosen.append(PlatformEnum(p))
            except ValueError:
                pass
    twin = duplicates.existing_publication(session, brief, chosen, by_dest)
    return _published_redirect(twin.brief_id, duplicate=True) if twin is not None else None


# ==================== Languages & destinations in the post forms ====================

def _lang_fields(cfg, draft) -> list:
    """One text field per language of Settings → Languages (the first: the one you write in), from the draft."""
    from app.services import languages
    draft = draft or {}
    texts = dict(draft.get('texts') or {})
    for lang in languages.MIRRORED:
        if draft.get(f'text_{lang}') and not texts.get(lang):
            texts[lang] = draft[f'text_{lang}']
    return [{'code': c, 'name': languages.name(c), 'text': texts.get(c) or ''} for c in languages.configured(cfg)]


def _dest_groups(form: str, cfg, draft, default_off: tuple = ()) -> list:
    """The form's destination checkboxes grouped by network (modules that are off and destinations turned off
    are not there). Ticked: what the draft had, else every destination that is set up — except video-only ones
    on the post form and the modules in `default_off` (as the old checkboxes were)."""
    from app.services import destinations
    groups = destinations.for_form(form, cfg)
    picked = (draft or {}).get('destinations')
    for g in groups:
        for d in g['destinations']:
            d['checked'] = (d['id'] in picked) if isinstance(picked, list) else \
                (d['ready'] and not g['video_only'] and g['module'] not in default_off)
    return groups


def _destination_targets(ids, form: str, brief, skipped: list) -> list:
    """The destinations a form sent → [(destination, PlatformEnum)] to create posts for. Turned off, removed,
    of a module that is off, or taking only a language the post has no text in: skipped (the result page says so)."""
    from app.services import destinations
    chosen, off = destinations.chosen(ids, form)
    skipped += [f"{i} (turned off or removed)" for i in off]
    out = []
    for d in chosen:
        if destinations.lacks_own_text(d, brief):
            skipped.append(f"{destinations.title(d)} (no {d['lang'].upper()} text)")
            continue
        out.append((d, PlatformEnum(destinations.post_platform(d, form))))
    return out


def _mark_allowed(payload: dict, allow_duplicate: Optional[str]) -> dict:
    """A ticked "publish again" also lets the post through the worker's duplicate guard."""
    if allow_duplicate:
        from app.services import duplicates
        payload[duplicates.ALLOW] = True
    return payload


@router.get('/published/{brief_id}', response_class=HTMLResponse)
def published_page(request: Request, brief_id: int, skipped: str = '', duplicate: str = '',
                   session: Session = Depends(get_session)):
    from sqlmodel import select
    posts = session.exec(select(Post).where(Post.brief_id == brief_id).order_by(Post.id)).all()
    return templates.TemplateResponse('news_success.html', {
        'request': request,
        'brief_id': brief_id,
        'num_posts': len(posts),
        'posts': posts,
        'skipped': [s for s in skipped.split(',') if s],
        'duplicate': bool(duplicate),
    })


@router.post('/news')
def submit_news(
    request: Request,
    text_ru: str = Form(...),
    text_en: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    platforms: Optional[List[str]] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    media: Optional[UploadFile] = File(None),
    media_normal: Optional[str] = Form(None),
    media_vertical: Optional[str] = Form(None),
    gallery_order: Optional[str] = Form(None),
    reddit_sentences: Optional[int] = Form(2),
    yt_title: Optional[str] = Form(None),
    x_split: Optional[str] = Form(None),
    allow_duplicate: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    media_path: Optional[str] = None
    if media and media.filename:
        import os, time
        os.makedirs('media', exist_ok=True)
        name_part = str(int(time.time()))
        safe_name = media.filename.replace('..', '').replace('/', '_').replace('\\', '_')
        media_path = os.path.join('media', f"{name_part}_{safe_name}")
        with open(media_path, 'wb') as f:
            f.write(media.file.read())
        # Persist relative path for reloading
        rel = media_path.replace('\\', '/')
        rel = rel.split('media/')[-1]
        save_draft({"media_path": rel})
    # override from hidden fields if present
    if media_normal:
        media_path = f"media/{media_normal}" if not media_normal.startswith('media/') else media_normal

    # Merge weekday auto-tags into tags before creating brief
    weekday_auto = []
    if scheduled_at:
        try:
            when_dt = datetime.fromisoformat(scheduled_at)
            if when_dt.weekday() == 5:
                weekday_auto.append('#screenshotsaturday')
            if when_dt.weekday() == 2:
                weekday_auto.append('#wishlistwednesday')
        except Exception:
            pass
    tags_final = (tags or '').strip()
    if weekday_auto:
        tags_final = (tags_final + ' ' + ' '.join(weekday_auto)).strip()

    # Persist gallery list into draft for continuity across refreshes
    if gallery_order is not None:
        try:
            save_draft({"gallery_normal": [x.strip() for x in (gallery_order or '').split(',') if x.strip()]})
        except Exception:
            pass

    # Persist Reddit sentence count in draft for UX continuity
    try:
        n = max(1, min(10, int(reddit_sentences or 2)))
        save_draft({"reddit_sentences": n})
    except Exception:
        pass
    # Persist YouTube header/title in draft
    try:
        if (yt_title or '').strip():
            save_draft({"yt_title": (yt_title or '').strip()})
    except Exception:
        pass

    brief = Brief(
        user_id=1,
        type='news',
        text_ru=text_ru or '',
        text_en=text_en or '',
        title=(yt_title or '').strip() or None,
        tags_json=tags_final,
        media_path=media_path,
    )
    dup = _duplicate_redirect(session, brief, platforms, allow_duplicate)
    if dup is not None:
        return dup
    session.add(brief)
    session.commit()
    session.refresh(brief)

    created_posts: List[Post] = []
    skipped: List[str] = []
    cfg = load_settings()
    def is_ready(plat: str) -> bool:
        if plat == 'discord':
            return bool(((cfg.get('discord_webhook_ru') or cfg.get('discord_webhook_en') or cfg.get('discord_webhook_url') or '').strip()))
        if plat == 'x':
            return bool((cfg.get('x_access_token') or '').strip())
        if plat == 'vk':
            return bool(((cfg.get('vk_access_token') or '').strip()) and ((cfg.get('vk_group_id') or '').strip()))
        if plat == 'yt':
            return bool(((cfg.get('yt_access_token') or '').strip()) and ((cfg.get('yt_refresh_token') or '').strip()))
        if plat == 'fb_ru':
            return bool(((cfg.get('fb_ru_page_id') or '').strip()) and ((cfg.get('fb_ru_page_access_token') or '').strip()))
        if plat == 'fb_en':
            return bool(((cfg.get('fb_en_page_id') or '').strip()) and ((cfg.get('fb_en_page_access_token') or '').strip()))
        if plat == 'reddit':
            return bool(((cfg.get('reddit_client_id') or '').strip()) and ((cfg.get('reddit_client_secret') or '').strip()) and ((cfg.get('reddit_username') or '').strip()) and ((cfg.get('reddit_password') or '').strip()) and ((cfg.get('reddit_subreddit_en') or '').strip()))
        if plat == 'tg':
            return bool(((cfg.get('tg_bot_token') or '').strip()) and ((cfg.get('tg_chat_id_ru') or '').strip()))
        if plat == 'bluesky':
            return bool(((cfg.get('bluesky_handle') or '').strip()) and ((cfg.get('bluesky_app_password') or '').strip()))
        if plat == 'tiktok':
            return True
        return False
    if platforms:
        # Detect if input media is a video (normal or vertical)
        has_video: bool = False
        has_vertical_video: bool = False
        has_normal_video: bool = False
        try:
            import mimetypes as _m
            import os as _os
            cand_vert = (media_vertical or '').strip() or None
            cand_norm = (media_normal or '').strip() or None
            # If uploaded directly this request, media_path has absolute/relative path under media/
            if (media_path or '').strip() and not cand_norm:
                cand_norm = (media_path.replace('media/', '') if media_path.startswith('media/') else media_path)
            def _ctype(candidate: str | None) -> str:
                if not candidate:
                    return ''
                name = candidate
                if not (name.startswith('media/') or _os.path.isabs(name)):
                    name = f"media/{name}"
                return _m.guess_type(name)[0] or ''
            ctv = _ctype(cand_vert)
            ctn = _ctype(cand_norm)
            has_vertical_video = bool(ctv.startswith('video/'))
            has_normal_video = bool(ctn.startswith('video/'))
            has_video = bool(ctv.startswith('video/') or ctn.startswith('video/'))
        except Exception:
            has_video = False
            has_vertical_video = False
            has_normal_video = False
        when_dt: Optional[datetime] = None
        if scheduled_at:
            try:
                # Interpret as local machine time coming from the browser (naive)
                when_dt = datetime.fromisoformat(scheduled_at)
            except Exception:
                when_dt = None
        for p in platforms:
            try:
                enum_val = PlatformEnum(p)
            except ValueError:
                continue
            # Skip unconfigured platforms and record the info
            if not is_ready(p):
                skipped.append(f"{p} (not configured)")
                continue
            # Failsafe: skip platforms lacking required media
            if p == 'tiktok':
                if not has_vertical_video:
                    continue
            if p == 'yt':
                # Only allow if there is a non-vertical (normal) video present
                if not has_normal_video:
                    continue
            post = Post(
                user_id=1,
                brief_id=brief.id,
                platform=enum_val,
                status=PostStatusEnum.queued,
                scheduled_at=when_dt,
            )
            session.add(post)
            created_posts.append(post)
        session.commit()

    # Enqueue publish tasks for all posts; if scheduled_at is in the past, run immediately
    for post in created_posts:
        import json as _json
        media_rel = None
        try:
            if str(post.platform) in (str(PlatformEnum.yt), 'yt'):
                # For YouTube, prefer normal (non-vertical) video only
                media_rel = (media_normal or '').strip() or None
            else:
                media_rel = (media_normal or '').strip() or None
        except Exception:
            media_rel = None
        payload = {"post_id": post.id}
        # Attach gallery list for platforms that support multiple images; clients will decide how to use it
        try:
            if (gallery_order or '').strip():
                glist = [x.strip() for x in (gallery_order or '').split(',') if x.strip()]
                if glist:
                    payload["gallery"] = glist
        except Exception:
            pass
        if media_rel:
            payload["media_rel"] = media_rel
        if str(post.platform) in (str(PlatformEnum.yt), 'yt') and (yt_title or '').strip():
            payload["yt_title"] = (yt_title or '').strip()
        # X thread option
        if str(post.platform) in (str(PlatformEnum.x), 'x'):
            payload["x_thread"] = bool(x_split)
        # Pass reddit_sentences for Reddit thread behavior
        try:
            if str(post.platform) in (str(PlatformEnum.reddit), 'reddit'):
                rn = max(1, min(10, int(reddit_sentences or 2)))
                payload["reddit_sentences"] = rn
        except Exception:
            pass
        _mark_allowed(payload, allow_duplicate)
        job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
        session.add(job)
        session.commit()
        session.refresh(job)
        # Compute delay based on local now
        try:
            from datetime import datetime as _dt
            # Use local naive time; if scheduled_at has tzinfo, convert to naive local approximation
            target = post.scheduled_at
            if target is not None and getattr(target, 'tzinfo', None) is not None:
                try:
                    target = target.replace(tzinfo=None)
                except Exception:
                    pass
            if target is not None:
                delay_s = int(max(0, (target - _dt.now()).total_seconds()))
            else:
                delay_s = 0
        except Exception:
            delay_s = 0
        publish_news_post.schedule(args=(job.id, post.id), delay=delay_s)

    # Clear draft on successful queue
    clear_draft()

    return _published_redirect(brief.id, skipped)


# ==================== Create Post / Video Pages ====================

@router.get('/create/post', response_class=HTMLResponse)
def create_post_form(request: Request):
    """Simplified form for image posts to VK, X, Discord, FB, Reddit, Telegram."""
    draft = load_draft()
    cfg = load_settings()
    return templates.TemplateResponse('create_post.html', {
        'request': request,
        'draft': draft,
        'langs': _lang_fields(cfg, draft),
        'dest_groups': _dest_groups('post', cfg, draft),
        'ui_language': cfg.get('ui_language', 'en')
    })


@router.post('/create/post')
def submit_post(
    request: Request,
    texts: dict = Depends(form_texts),
    tags: Optional[str] = Form(None),
    platforms: Optional[List[str]] = Form(None),
    destinations: Optional[List[str]] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    media_normal: Optional[str] = Form(None),
    gallery_order: Optional[str] = Form(None),
    reddit_sentences: Optional[int] = Form(2),
    x_split: Optional[str] = Form(None),
    project_id: Optional[str] = Form(None),
    goal_id: Optional[str] = Form(None),
    allow_duplicate: Optional[str] = Form(None),
    yt_title: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Process image post submission."""
    # Unified screenshots field: the FIRST uploaded image is the "main" image
    # that goes to every platform; the rest ride along as a gallery where the
    # platform API supports multiple images. Deriving main from gallery[0] here
    # is what stops a gallery-only post from publishing with no image at all.
    glist = [x.strip() for x in (gallery_order or '').split(',') if x.strip()]
    main_rel = (media_normal or '').strip() or (glist[0] if glist else None)
    media_path = None
    if main_rel:
        media_path = f"media/{main_rel}" if not main_rel.startswith('media/') else main_rel

    # Weekday auto-tags
    tags_final = (tags or '').strip()
    if scheduled_at:
        try:
            when_dt = datetime.fromisoformat(scheduled_at)
            if when_dt.weekday() == 5:
                tags_final = (tags_final + ' #screenshotsaturday').strip()
            if when_dt.weekday() == 2:
                tags_final = (tags_final + ' #wishlistwednesday').strip()
        except Exception:
            pass

    brief = Brief(
        user_id=1,
        type='post',
        text_ru=texts.get('ru') or '',
        text_en=texts.get('en') or '',
        tags_json=tags_final,
        media_path=media_path,
    )
    from app.services.languages import set_brief_texts
    set_brief_texts(brief, texts)  # text_<lang> of every language of Settings → Languages
    skipped: List[str] = []
    # The form sends destinations (each with its language); `platforms` is the older way (API, scripts):
    # posts without a destination, published by the platform's old rules.
    targets = _destination_targets(destinations, 'post', brief, skipped) if destinations else None
    dup = _duplicate_redirect(session, brief, platforms, allow_duplicate, targets)
    if dup is not None:
        return dup  # the draft stays, so "publish again" is one tick away
    session.add(brief)
    session.commit()
    session.refresh(brief)
    from app.services.marketing import tag_brief
    tag_brief(session, brief, project_id, goal_id)

    created_posts: List[Post] = []
    cfg = load_settings()

    when_dt: Optional[datetime] = None
    if scheduled_at:
        try:
            when_dt = datetime.fromisoformat(scheduled_at)
        except Exception:
            when_dt = None

    if targets is not None:
        for dest, enum_val in targets:
            post = Post(
                user_id=1,
                brief_id=brief.id,
                platform=enum_val,
                destination_id=dest['id'],
                status=PostStatusEnum.queued,
                scheduled_at=when_dt,
            )
            session.add(post)
            created_posts.append(post)
        session.commit()
    elif platforms:
        for p in platforms:
            try:
                enum_val = PlatformEnum(p)
            except ValueError:
                continue
            post = Post(
                user_id=1,
                brief_id=brief.id,
                platform=enum_val,
                status=PostStatusEnum.queued,
                scheduled_at=when_dt,
            )
            session.add(post)
            created_posts.append(post)
        session.commit()

    # Enqueue publish tasks
    for post in created_posts:
        import json as _json
        payload = {"post_id": post.id}
        if glist:
            payload["gallery"] = glist
        if main_rel:
            payload["media_rel"] = main_rel
        if str(post.platform) in (str(PlatformEnum.x), 'x'):
            payload["x_thread"] = bool(x_split)
        if str(post.platform) in (str(PlatformEnum.reddit), 'reddit'):
            payload["reddit_sentences"] = max(1, min(10, int(reddit_sentences or 2)))
        # YouTube from the post page (only offered for a video): title of its own,
        # not Brief.title — Reddit would take that as its post title
        if str(post.platform) in (str(PlatformEnum.yt), 'yt') and (yt_title or '').strip():
            payload["yt_title"] = yt_title.strip()[:100]
        _mark_allowed(payload, allow_duplicate)

        job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
        session.add(job)
        session.commit()
        session.refresh(job)

        delay_s = 0
        if when_dt:
            from datetime import datetime as _dt
            delay_s = int(max(0, (when_dt - _dt.now()).total_seconds()))
        publish_news_post.schedule(args=(job.id, post.id), delay=delay_s)

    clear_draft()

    return _published_redirect(brief.id, skipped)


@router.get('/shorts', response_class=HTMLResponse)
def shorts_form(request: Request):
    """The Shorts Factory: batch-queue vertical short videos."""
    cfg = load_settings()
    return templates.TemplateResponse('shorts.html', {
        'request': request,
        'ui_language': cfg.get('ui_language', 'en'),
    })


def _safe_filename(name: str) -> str:
    import re
    base = (name or 'clip.mp4').replace('\\', '/').split('/')[-1]
    base = re.sub(r'[^A-Za-z0-9._-]+', '_', base).strip('_') or 'clip.mp4'
    return base[:80]


@router.post('/shorts')
def submit_shorts(
    request: Request,
    files: List[UploadFile] = File(default=[]),
    meta: str = Form('[]'),
    session: Session = Depends(get_session),
):
    """Accept a batch of clips + per-card metadata and enqueue a render_short job per
    clip. Posts are created later by render_short (post-render).

    The page uploads every clip as soon as it is dropped (/api/shorts/upload) and sends
    `src_rel` + the video editor's `spec` per card; `files` is the older path (the clip
    in this request, settings as separate fields), kept for scripts and old tabs."""
    import json as _json, os as _os
    from uuid import uuid4
    from app.jobs.tasks import render_short
    from app.services import music_library, shorts_upload, video_edit
    from app.services.text_overlay import clean_overlays, clean_trim

    try:
        metas = _json.loads(meta or '[]') or []
    except Exception:
        metas = []
    if not isinstance(metas, list):
        metas = []

    def _num(v, default=0.0):
        try:
            return float(v)
        except Exception:
            return float(default)

    _os.makedirs('media', exist_ok=True)
    uploads = iter(files or [])
    extra = len(files or []) - sum(1 for m in metas if not (isinstance(m, dict) and m.get('src_rel')))
    metas += [{}] * max(0, extra)  # a clip without its settings still goes, as before
    allowed_music = None
    created = 0
    for i, m in enumerate(metas):
        m = m if isinstance(m, dict) else {}
        spec = None
        if m.get('src_rel'):
            try:
                fname = shorts_upload.uploaded_rel(m['src_rel'])
            except video_edit.EditError:
                continue
            if isinstance(m.get('spec'), dict):
                info = video_edit.probe(video_edit.media_abs(fname))
                spec = video_edit.short_spec(m['spec'], info['duration'])
                if spec['music']:
                    if allowed_music is None:
                        allowed_music = music_library.preset_music_paths(session)
                    if not music_library.is_allowed(spec['music']['path'], extra=allowed_music):
                        spec['music'] = None
        else:
            up = next(uploads, None)
            if up is None:
                continue
            try:
                raw = up.file.read()
            except Exception:
                raw = b''
            if not raw:
                continue
            fname = f"short_{uuid4().hex[:12]}_{_safe_filename(up.filename or f'clip_{i}.mp4')}"
            with open(_os.path.join('media', fname), 'wb') as fh:
                fh.write(raw)

        if spec is not None:
            # The editor's settings, mirrored into the old columns for anything that reads them
            trim_start, trim_end, overlays = spec['trim_start'], spec['trim_end'], spec['overlays']
            music = spec['music'] or {}
            crop = dict(crop_x=int(round(spec['crop_x'] * 100)), crop_y=int(round(spec['crop_y'] * 100)),
                        crop_scale=spec['zoom'], music_path=music.get('path'),
                        music_offset_sec=int(music.get('start') or 0),
                        fade_in_ms=int((music.get('fade_in') or 0) * 1000),
                        fade_out_ms=int((music.get('fade_out') or 0) * 1000))
        else:
            # Editor: trim (source seconds) + text overlays (relative to the trimmed clip)
            trim_start, trim_end = clean_trim(m.get('trim_start'), m.get('trim_end'))
            overlays = clean_overlays(m.get('overlays'), (trim_end - (trim_start or 0)) if trim_end else None)
            crop = dict(crop_x=int(_num(m.get('crop_x'), 50)), crop_y=int(_num(m.get('crop_y'), 50)),
                        crop_scale=_num(m.get('zoom'), 1.0), music_path=(m.get('music_path') or None),
                        music_offset_sec=int(_num(m.get('music_offset'), 0)),
                        fade_in_ms=int(_num(m.get('fade_in'), 0) * 1000),
                        fade_out_ms=int(_num(m.get('fade_out'), 0) * 1000))

        brief = Brief(
            user_id=1,
            type='video',
            text_ru=(m.get('text_ru') or ''),
            text_en=(m.get('text_en') or ''),
            title=(m.get('title') or '').strip() or None,
            tags_json=(m.get('tags') or '').strip(),
            media_path=fname,
            vertical_916=True,  # marks this Brief as a Shorts-Factory item
            trim_start=trim_start,
            trim_end=trim_end,
            overlays_json=(_json.dumps(overlays, ensure_ascii=False) if overlays else None),
            edit_spec_json=(_json.dumps(spec, ensure_ascii=False) if spec is not None else None),
            **crop,
        )
        session.add(brief)
        session.commit()
        session.refresh(brief)
        from app.services.marketing import tag_brief
        tag_brief(session, brief, m.get('project_id'), m.get('goal_id'))

        platforms = [p for p in (m.get('platforms') or []) if p]
        payload = {
            'brief_id': brief.id,
            'platforms': platforms,
            'scheduled_at': (m.get('scheduled_at') or None),
        }
        rjob = Job(kind=JobKindEnum.render, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
        session.add(rjob)
        session.commit()
        session.refresh(rjob)
        render_short.schedule(args=(rjob.id, brief.id), delay=0)
        created += 1

    from fastapi.responses import RedirectResponse
    return RedirectResponse('/shorts/board', status_code=303)


@router.get('/shorts/board', response_class=HTMLResponse)
def shorts_board(request: Request):
    """Preview/status board for the Shorts Factory."""
    cfg = load_settings()
    return templates.TemplateResponse('shorts_board.html', {
        'request': request,
        'ui_language': cfg.get('ui_language', 'en'),
    })


@router.get('/shorts/board/partial', response_class=HTMLResponse)
def shorts_board_partial(request: Request, session: Session = Depends(get_session)):
    """The auto-refreshing board content (mirrors the /jobs/partial pattern)."""
    from sqlmodel import select
    import json as _json, os as _os
    briefs = session.exec(
        select(Brief).where(Brief.vertical_916 == True).order_by(Brief.id.desc()).limit(50)
    ).all()
    # Map the latest render job per brief_id (single pass, avoids N+1).
    render_jobs = session.exec(
        select(Job).where(Job.kind == JobKindEnum.render).order_by(Job.id.desc()).limit(300)
    ).all()
    rj_by_brief = {}
    for j in render_jobs:
        try:
            bid = (_json.loads(j.payload_json or '{}') or {}).get('brief_id')
            if bid and bid not in rj_by_brief:
                rj_by_brief[bid] = j
        except Exception:
            pass

    def _first(d, *keys):
        for k in keys:
            v = d.get(k)
            if v is not None:
                return v
        return None

    def _human(n):
        try:
            n = int(n)
        except Exception:
            return None
        if n >= 1_000_000:
            return f"{n/1_000_000:.1f}M".replace('.0M', 'M')
        if n >= 1_000:
            return f"{n/1_000:.1f}K".replace('.0K', 'K')
        return str(n)

    cards = []
    total_views = 0
    published_count = 0
    for b in briefs:
        posts = session.exec(select(Post).where(Post.brief_id == b.id)).all()
        poster_url = None
        if b.media_path:
            base = _os.path.splitext(str(b.media_path).replace('\\', '/').split('/')[-1])[0]
            if _os.path.exists(_os.path.join('media', f"{base}_poster.jpg")):
                poster_url = f"/media/{base}_poster.jpg"
        vertical_url = None
        if b.media_vertical_path:
            vname = str(b.media_vertical_path).replace('\\', '/').split('/')[-1]
            vertical_url = f"/media/{vname}"
        rj = rj_by_brief.get(b.id)
        render_status = None
        if rj is not None:
            render_status = rj.status.value if hasattr(rj.status, 'value') else str(rj.status)
        render_error = (getattr(rj, 'last_error', None) or '') if rj is not None else ''
        # Full caption for the TikTok copy button: title + description + tags
        _parts = [b.title or '', (b.text_en or b.text_ru or '')]
        caption = '\n'.join(p.strip() for p in _parts if p and p.strip()).strip()
        if b.tags_json:
            caption = (caption + '\n' + b.tags_json).strip()
        chips = []
        for p in posts:
            mj = {}
            if p.metrics_json:
                try:
                    mj = _json.loads(p.metrics_json) or {}
                except Exception:
                    mj = {}
            views = _first(mj, 'views', 'view', 'viewCount')
            likes = _first(mj, 'likes', 'like', 'likeCount')
            st = (p.status.value if hasattr(p.status, 'value') else str(p.status))
            if st == 'done':
                published_count += 1
                try:
                    total_views += int(views or 0)
                except Exception:
                    pass
            chips.append({
                'platform': (p.platform.value if hasattr(p.platform, 'value') else str(p.platform)),
                'status': st,
                'url': p.platform_url,
                'error': (p.error_text or ''),
                'views_fmt': _human(views),
                'likes_fmt': _human(likes),
            })
        cards.append({
            'brief_id': b.id,
            'title': b.title or '',
            'poster_url': poster_url,
            'vertical_url': vertical_url,
            'render_status': render_status,
            'render_error': render_error,
            'rendered': bool(b.media_vertical_path),
            'caption': caption,
            'chips': chips,
        })
    cfg = load_settings()
    return templates.TemplateResponse('partials/shorts_board.html', {
        'request': request,
        'cards': cards,
        'total_views': total_views,
        'total_views_fmt': (_human(total_views) or '0'),
        'published_count': published_count,
        'ui_language': cfg.get('ui_language', 'en'),
    })


@router.get('/shorts/repost/{brief_id}', response_class=HTMLResponse)
def shorts_repost_page(brief_id: int, request: Request, session: Session = Depends(get_session)):
    """Repost a published short to more networks: a caption per network, auto-translated."""
    from fastapi.responses import RedirectResponse
    from app.services import repost
    brief = session.get(Brief, brief_id)
    if brief is None or not brief.vertical_916:
        return RedirectResponse('/shorts/board', status_code=303)
    cfg = load_settings()
    return templates.TemplateResponse('shorts_repost.html', {
        'request': request,
        'ctx': repost.page_context(session, brief, cfg),
        'ui_language': cfg.get('ui_language', 'en'),
    })


@router.post('/shorts/board/refresh_metrics', response_class=HTMLResponse)
def shorts_board_refresh_metrics(request: Request, session: Session = Depends(get_session)):
    """Kick a metrics refresh for all published shorts, then re-render the board."""
    from sqlmodel import select
    briefs = session.exec(
        select(Brief).where(Brief.vertical_916 == True).order_by(Brief.id.desc()).limit(50)
    ).all()
    bids = [b.id for b in briefs]
    if bids:
        posts = session.exec(
            select(Post).where(Post.brief_id.in_(bids), Post.status == PostStatusEnum.done)
        ).all()
        for p in posts:
            try:
                refresh_metrics_for_post.schedule(args=(p.id,), delay=0)
            except Exception:
                pass
    return shorts_board_partial(request, session)


@router.get('/create/video', response_class=HTMLResponse)
def create_video_form(request: Request):
    """Form for video posts to YouTube, TikTok, VK Video."""
    draft = load_draft()
    cfg = load_settings()
    return templates.TemplateResponse('create_video.html', {
        'request': request,
        'draft': draft,
        'langs': _lang_fields(cfg, draft),
        # VK Video was never ticked by default; YouTube and TikTok were
        'dest_groups': _dest_groups('video', cfg, None, default_off=('vk',)),
        'ui_language': cfg.get('ui_language', 'en')
    })


@router.post('/create/video')
def submit_video(
    request: Request,
    title: Optional[str] = Form(None),
    texts: dict = Depends(form_texts),
    tags: Optional[str] = Form(None),
    platforms: Optional[List[str]] = Form(None),
    destinations: Optional[List[str]] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    media_horizontal: Optional[str] = Form(None),
    media_vertical: Optional[str] = Form(None),
    yt_playlist: Optional[str] = Form(None),
    yt_privacy: Optional[str] = Form('public'),
    project_id: Optional[str] = Form(None),
    goal_id: Optional[str] = Form(None),
    allow_duplicate: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Process video post submission."""
    # Horizontal for YouTube/VK Video (vertical when there's no 16:9: a Short), vertical for TikTok
    media_horizontal = (media_horizontal or '').strip() or None
    media_vertical = (media_vertical or '').strip() or None
    media_path = media_horizontal or media_vertical
    if media_path and not media_path.startswith('media/'):
        media_path = f"media/{media_path}"

    brief = Brief(
        user_id=1,
        type='video',
        text_ru=texts.get('ru') or '',
        text_en=texts.get('en') or '',
        title=(title or '').strip() or None,
        tags_json=(tags or '').strip(),
        media_path=media_path,
        # TikTok reads the brief's vertical file, not the job payload
        media_vertical_path=media_vertical,
        media_normal_path=media_horizontal,
    )
    from app.services.languages import set_brief_texts
    set_brief_texts(brief, texts)
    skipped: List[str] = []
    # destinations from the form (each with its language); `platforms` = the older way, no destination
    targets = _destination_targets(destinations, 'video', brief, skipped) if destinations else None
    dup = _duplicate_redirect(session, brief, platforms, allow_duplicate, targets)
    if dup is not None:
        return dup
    session.add(brief)
    session.commit()
    session.refresh(brief)
    from app.services.marketing import tag_brief
    tag_brief(session, brief, project_id, goal_id)

    created_posts: List[Post] = []

    when_dt: Optional[datetime] = None
    if scheduled_at:
        try:
            when_dt = datetime.fromisoformat(scheduled_at)
        except Exception:
            when_dt = None

    if targets is not None:
        chosen = [(d['id'], plat) for d, plat in targets]
    else:
        chosen = []
        for p in platforms or []:
            try:
                chosen.append((None, PlatformEnum(p)))
            except ValueError:
                continue
    if chosen:
        for dest_id, enum_val in chosen:
            p = enum_val.value

            # Skip if no media for platform
            if p == 'tiktok' and not media_vertical:
                continue
            if p in ('yt', 'vk_video') and not (media_horizontal or media_vertical):
                continue

            post = Post(
                user_id=1,
                brief_id=brief.id,
                platform=enum_val,
                destination_id=dest_id,
                status=PostStatusEnum.queued,
                scheduled_at=when_dt,
            )
            session.add(post)
            created_posts.append(post)
        session.commit()

    # Enqueue publish tasks
    for post in created_posts:
        import json as _json
        payload = {"post_id": post.id}

        if str(post.platform) in (str(PlatformEnum.yt), 'yt'):
            if media_horizontal or media_vertical:
                payload["media_rel"] = media_horizontal or media_vertical
            if title:
                payload["yt_title"] = title
            # always sent: '' means the user picked "No playlist"
            payload["yt_playlist"] = (yt_playlist or "").strip()
            if yt_privacy:
                payload["yt_privacy"] = yt_privacy
        elif str(post.platform) in (str(PlatformEnum.tiktok), 'tiktok'):
            if media_vertical:
                payload["media_rel"] = media_vertical
        else:
            if media_horizontal or media_vertical:
                payload["media_rel"] = media_horizontal or media_vertical
        _mark_allowed(payload, allow_duplicate)

        job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
        session.add(job)
        session.commit()
        session.refresh(job)

        delay_s = 0
        if when_dt:
            from datetime import datetime as _dt
            delay_s = int(max(0, (when_dt - _dt.now()).total_seconds()))
        publish_news_post.schedule(args=(job.id, post.id), delay=delay_s)

    clear_draft()

    return _published_redirect(brief.id, skipped)
