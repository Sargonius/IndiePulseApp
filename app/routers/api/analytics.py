"""Analytics table and metrics refresh endpoints."""

from app.db.session import get_session
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session
from typing import Optional
from app.routers.api.common import templates_api

router = APIRouter()


def _analytics_rows(session):
    """The 100 newest posts, minus IG/TikTok drafts left in the platform's inbox
    ('awaiting_finish') — those never actually went out."""
    from sqlmodel import select
    from app.models.models import Post, PostStatusEnum
    return session.exec(
        select(Post).where(Post.status != PostStatusEnum.awaiting_finish)
        .order_by(Post.created_at.desc()).limit(100)
    ).all()


@router.get("/analytics/table", response_class=HTMLResponse)
def analytics_table(request: Request, session: Session = Depends(get_session)):
    rows = _analytics_rows(session)
    # Reuse pages enrichment to attach preview/thumb and mapped metrics
    try:
        from app.routers.pages import _enrich_posts as _ep
        data = _ep(rows)
    except Exception:
        data = []
    return templates_api.TemplateResponse('partials/analytics_table.html', {'request': request, 'posts': data})


@router.post("/analytics/refresh", response_class=HTMLResponse)
def analytics_refresh(request: Request, session: Session = Depends(get_session)):
    from app.models.models import PostStatusEnum
    from app.platforms import platform_key
    rows = _analytics_rows(session)
    # Published YouTube videos go in one batched videos.list call instead of one each
    yt_ids = [p.id for p in rows if platform_key(p.platform) == 'yt'
              and p.status == PostStatusEnum.done and p.platform_post_id]
    ids = [p.id for p in rows if p.id not in yt_ids]
    # Run metrics refresh synchronously
    try:
        from app.jobs.tasks import refresh_metrics_for_post as _refresh, refresh_youtube_metrics
        if yt_ids:
            try:
                refresh_youtube_metrics.call_local(yt_ids)
            except Exception:
                pass
        for pid in ids:
            try:
                _refresh.call_local(pid)
            except Exception:
                pass
    except Exception:
        pass
    # Return fresh table
    return analytics_table(request, session)


@router.post("/analytics/refresh_one", response_class=HTMLResponse)
def analytics_refresh_one(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    pid = id or id_q
    if not pid:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    # Refresh this single post's metrics synchronously so fresh data is visible
    # immediately, then re-render the analytics table (NOT the posts table).
    try:
        from app.jobs.tasks import refresh_metrics_for_post as _refresh
        _refresh.call_local(pid)
    except Exception:
        pass
    return analytics_table(request, session)


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
