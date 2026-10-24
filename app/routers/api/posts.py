"""Posts: listing, scheduling, editing, publish-now, remote delete."""

import httpx
from app.db.session import get_session
from app.models.models import Post
from app.services.settings import load_settings
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session
from sqlmodel import select
from typing import Optional
from app.routers.api.common import templates_api
from app.routers.api.x import _x_ensure_token

router = APIRouter()

@router.get("/posts")
def list_posts(limit: int = 10, session: Session = Depends(get_session)):
    rows = session.exec(select(Post).order_by(Post.created_at.desc()).limit(limit)).all()
    return rows


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

    # keep the media/gallery/options chosen at creation time
    from app.jobs.tasks import publish_payload_for
    payload = publish_payload_for(session, post.id)
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
            from app.platforms.facebook import page_of
            page_token = page_of(post, cfg)[1]  # its destination's page, else the RU / EN one
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
            from app.platforms.telegram import chat_of
            token = (cfg.get('tg_bot_token') or '').strip()
            chat_id = chat_of(post, cfg)  # its destination's chat, else the original channel
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
