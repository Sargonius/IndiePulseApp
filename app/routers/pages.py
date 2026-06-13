from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Request, Form, Depends, UploadFile, File
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app.db.session import get_session
from app.models.models import Brief, Post, PlatformEnum, PostStatusEnum, Job, JobKindEnum, JobStatusEnum, Project
from app.jobs.tasks import publish_news_post
from app.services.draft_store import load_draft, save_draft, clear_draft
from app.services.settings import load_settings
from app.jobs.tasks import refresh_metrics_for_post
from app.services.settings import load_settings

router = APIRouter()
templates = Jinja2Templates(directory='app/templates')

def _enrich_posts(posts):
    import json as _json
    from sqlmodel import Session as _Session
    from app.db.session import engine as _engine
    from app.models.models import Brief as _Brief
    enriched = []
    for p in posts:
        metrics = {}
        if getattr(p, 'metrics_json', None):
            try:
                metrics = _json.loads(p.metrics_json) or {}
            except Exception:
                metrics = {}
        def pick(*keys):
            for k in keys:
                if metrics.get(k) is not None:
                    return metrics.get(k)
            return None
        # Build preview and tags from brief
        preview = ''
        tags_val = None
        thumb_url = None
        try:
            with _Session(_engine) as _s:
                b = _s.get(_Brief, getattr(p, 'brief_id', None))
                if b is not None:
                    base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                    preview = (base[:140] + ('…' if len(base) > 140 else ''))
                    tags_val = getattr(b, 'tags_json', None)
                    media_rel = getattr(b, 'media_path', None)
                    if media_rel:
                        name = str(media_rel).replace('\\','/').split('/')[-1]
                        thumb_url = f"/media/{name}"
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
        m_like = pick('like', 'likes')
        m_repost = pick('retweet', 'reposts', 'repost')
        m_reply = pick('reply', 'comments')
        m_quote = pick('quote')
        m_views = pick('views', 'impressions')
        # Total engagement: any direct reaction (views excluded — they are reach, not reaction)
        m_total = sum(v for v in (m_like, m_repost, m_reply, m_quote) if isinstance(v, int))
        has_metrics = any(isinstance(v, int) for v in (m_like, m_repost, m_reply, m_quote, m_views))
        row = {
            'id': p.id,
            'platform_text': platform_text,
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
        }
        enriched.append(row)
    return enriched

@router.get('/', response_class=HTMLResponse)
def index(request: Request):
    cfg = load_settings()
    return templates.TemplateResponse('index.html', {'request': request, 'body_class': 'page-index', 'ui_language': cfg.get('ui_language', 'en')})

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
    cfg = load_settings()
    return templates.TemplateResponse('projects.html', {
        'request': request,
        'projects': projects,
        'ui_language': cfg.get('ui_language', 'en'),
    })


@router.get('/accounts', response_class=HTMLResponse)
def accounts(request: Request):
    cfg = load_settings()
    token = cfg.get('vk_access_token', '') or ''
    token_hint = ('•••' + token[-4:]) if token else ''
    return templates.TemplateResponse(
        'accounts.html',
        {
            'request': request,
            'ui_language': cfg.get('ui_language', 'en'),
            'discord_webhook': cfg.get('discord_webhook_url', ''),
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
            'fb_app_id': cfg.get('fb_app_id', ''),
            'fb_app_secret': cfg.get('fb_app_secret', ''),
            'fb_ru_page_id': cfg.get('fb_ru_page_id', ''),
            'fb_en_page_id': cfg.get('fb_en_page_id', ''),
            'fb_ru_page_access_token': cfg.get('fb_ru_page_access_token', ''),
            'fb_en_page_access_token': cfg.get('fb_en_page_access_token', ''),
            'bluesky_handle': cfg.get('bluesky_handle', ''),
            'bluesky_service': cfg.get('bluesky_service', 'https://bsky.social'),
            'bluesky_pw_hint': (('•••' + (cfg.get('bluesky_app_password') or '')[-4:]) if (cfg.get('bluesky_app_password') or '') else ''),
            'reddit_client_id': cfg.get('reddit_client_id', ''),
            'reddit_client_secret': cfg.get('reddit_client_secret', ''),
            'reddit_username': cfg.get('reddit_username', ''),
            'reddit_subreddit_en': cfg.get('reddit_subreddit_en', ''),
            'openai_api_key': cfg.get('openai_api_key', ''),
            'openai_model': cfg.get('openai_model', ''),
        },
    )

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
def analytics_list(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select
    rows = session.exec(select(Post).order_by(Post.created_at.desc()).limit(100)).all()
    data = _enrich_posts(rows)
    cfg = load_settings()
    return templates.TemplateResponse('analytics.html', {'request': request, 'posts': data, 'ui_language': cfg.get('ui_language', 'en')})


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
                skipped.append(p)
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

    return templates.TemplateResponse('news_success.html', {
        'request': request,
        'brief_id': brief.id,
        'num_posts': len(created_posts),
        'skipped': skipped,
    })


# ==================== Create Post / Video Pages ====================

@router.get('/create/post', response_class=HTMLResponse)
def create_post_form(request: Request):
    """Simplified form for image posts to VK, X, Discord, FB, Reddit, Telegram."""
    draft = load_draft()
    cfg = load_settings()
    return templates.TemplateResponse('create_post.html', {
        'request': request,
        'draft': draft,
        'ui_language': cfg.get('ui_language', 'en')
    })


@router.post('/create/post')
def submit_post(
    request: Request,
    text_ru: str = Form(...),
    text_en: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    platforms: Optional[List[str]] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    media_normal: Optional[str] = Form(None),
    gallery_order: Optional[str] = Form(None),
    reddit_sentences: Optional[int] = Form(2),
    x_split: Optional[str] = Form(None),
    session: Session = Depends(get_session),
):
    """Process image post submission."""
    media_path = None
    if media_normal:
        media_path = f"media/{media_normal}" if not media_normal.startswith('media/') else media_normal

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
        text_ru=text_ru or '',
        text_en=text_en or '',
        tags_json=tags_final,
        media_path=media_path,
    )
    session.add(brief)
    session.commit()
    session.refresh(brief)

    created_posts: List[Post] = []
    cfg = load_settings()

    when_dt: Optional[datetime] = None
    if scheduled_at:
        try:
            when_dt = datetime.fromisoformat(scheduled_at)
        except Exception:
            when_dt = None

    if platforms:
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
        if gallery_order:
            glist = [x.strip() for x in gallery_order.split(',') if x.strip()]
            if glist:
                payload["gallery"] = glist
        if media_normal:
            payload["media_rel"] = media_normal
        if str(post.platform) in (str(PlatformEnum.x), 'x'):
            payload["x_thread"] = bool(x_split)
        if str(post.platform) in (str(PlatformEnum.reddit), 'reddit'):
            payload["reddit_sentences"] = max(1, min(10, int(reddit_sentences or 2)))

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

    return templates.TemplateResponse('news_success.html', {
        'request': request,
        'brief_id': brief.id,
        'num_posts': len(created_posts),
        'skipped': [],
    })


@router.get('/create/video', response_class=HTMLResponse)
def create_video_form(request: Request):
    """Form for video posts to YouTube, TikTok, VK Video."""
    draft = load_draft()
    cfg = load_settings()
    return templates.TemplateResponse('create_video.html', {
        'request': request,
        'draft': draft,
        'ui_language': cfg.get('ui_language', 'en')
    })


@router.post('/create/video')
def submit_video(
    request: Request,
    title: Optional[str] = Form(None),
    text_ru: Optional[str] = Form(None),
    text_en: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    platforms: Optional[List[str]] = Form(None),
    scheduled_at: Optional[str] = Form(None),
    media_horizontal: Optional[str] = Form(None),
    media_vertical: Optional[str] = Form(None),
    yt_playlist: Optional[str] = Form(None),
    yt_privacy: Optional[str] = Form('public'),
    session: Session = Depends(get_session),
):
    """Process video post submission."""
    # Use horizontal for YouTube, vertical for TikTok/Shorts
    media_path = media_horizontal or media_vertical
    if media_path and not media_path.startswith('media/'):
        media_path = f"media/{media_path}"

    brief = Brief(
        user_id=1,
        type='video',
        text_ru=text_ru or '',
        text_en=text_en or '',
        title=(title or '').strip() or None,
        tags_json=(tags or '').strip(),
        media_path=media_path,
    )
    session.add(brief)
    session.commit()
    session.refresh(brief)

    created_posts: List[Post] = []

    when_dt: Optional[datetime] = None
    if scheduled_at:
        try:
            when_dt = datetime.fromisoformat(scheduled_at)
        except Exception:
            when_dt = None

    if platforms:
        for p in platforms:
            try:
                enum_val = PlatformEnum(p)
            except ValueError:
                continue

            # Skip if no media for platform
            if p == 'tiktok' and not media_vertical:
                continue
            if p in ('yt', 'vk_video') and not media_horizontal:
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

        if str(post.platform) in (str(PlatformEnum.yt), 'yt'):
            if media_horizontal:
                payload["media_rel"] = media_horizontal
            if title:
                payload["yt_title"] = title
            if yt_playlist:
                payload["yt_playlist"] = yt_playlist
            if yt_privacy:
                payload["yt_privacy"] = yt_privacy
        elif str(post.platform) in (str(PlatformEnum.tiktok), 'tiktok'):
            if media_vertical:
                payload["media_rel"] = media_vertical
        else:
            if media_horizontal:
                payload["media_rel"] = media_horizontal

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

    return templates.TemplateResponse('news_success.html', {
        'request': request,
        'brief_id': brief.id,
        'num_posts': len(created_posts),
        'skipped': [],
    })
