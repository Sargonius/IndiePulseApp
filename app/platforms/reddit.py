"""Reddit (PRAW)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    # A post without a destination: the EN subreddit with the EN text (as it always was)
    from app.services.reddit_client import RedditClient
    from app.services.settings import load_settings
    cfg = load_settings()
    cid = (cfg.get('reddit_client_id') or '').strip()
    csec = (cfg.get('reddit_client_secret') or '').strip()
    uname = (cfg.get('reddit_username') or '').strip()
    pwd = (cfg.get('reddit_password') or '').strip()
    from app.platforms.common import post_destination, post_text
    dest = post_destination(post, cfg)
    sub = ((dest['target'].get('subreddit') if dest else cfg.get('reddit_subreddit_en')) or '').strip()
    if not (cid and csec and uname and pwd and sub):
        post.status = PostStatusEnum.error
        post.error_text = 'reddit_not_configured'
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = 'reddit_not_configured'
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'
    from app.platforms.common import brief_for_publish
    brief = brief_for_publish(session, post)
    full_body = post_text(brief, post, ('en', 'ru'), cfg)
    # Use job payload value for number of sentences in main post
    sent_count = 2
    try:
        import json as _json
        if job and job.payload_json:
            sc = (_json.loads(job.payload_json) or {}).get('reddit_sentences')
            if sc is not None:
                sent_count = max(1, min(10, int(sc)))
    except Exception:
        sent_count = 2
    # Split by sentences and pack first N sentences as title; rest as comment
    import re as _re
    parts = _re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", full_body or '')
    head = ' '.join([p.strip() for p in parts[:sent_count]]).strip()
    rest_text = ' '.join([p.strip() for p in parts[sent_count:]]).strip()
    # Title fallback
    title = ((getattr(brief, 'title', None) or head or getattr(brief, 'text_en', None) or 'Post from IndiePulse'))[:290]
    body = ''  # main post body left empty; we will add the remainder as a comment
    if brief and getattr(brief, 'tags_json', None):
        try:
            body = (body + '\n' + brief.tags_json).strip()
        except Exception:
            pass
    try:
        rc = RedditClient(cid, csec, uname, pwd)
        # Use normal/horizontal media if present
        media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
        # Prefer override and then first gallery item
        try:
            import json as _json
            if job and job.payload_json:
                parsed = (_json.loads(job.payload_json) or {})
                override = parsed.get('media_rel')
                gallery_list = parsed.get('gallery') or []
                if override:
                    media_rel = override
                elif (not media_rel) and gallery_list:
                    media_rel = gallery_list[0]
        except Exception:
            pass
        url = None; sid = None
        if media_rel:
            import os as _os, mimetypes as _m
            p = media_rel.replace('\\','/')
            if not p.startswith('media/'):
                p = f"media/{p}"
            path = _os.path.abspath(p)
            ctype = _m.guess_type(path)[0] or ''
            if ctype.startswith('image/'):
                # Reddit supports jpg/png best; convert others to PNG
                if not (ctype.endswith('jpeg') or ctype.endswith('jpg') or ctype.endswith('png')):
                    try:
                        from PIL import Image
                        img = Image.open(path).convert('RGB')
                        tmp_path = _os.path.join('render', f"reddit_{post.id}_image.png")
                        _os.makedirs('render', exist_ok=True)
                        img.save(tmp_path, format='PNG', optimize=True)
                        path = _os.path.abspath(tmp_path)
                    except Exception:
                        pass
                sid, url = rc.submit_image(sub, title=title, image_path=path)
            elif ctype.startswith('video/'):
                # Direct video upload to Reddit; transcode to safe settings and add thumbnail
                try:
                    from app.services.reddit_media import prepare_video_for_reddit as _prep
                    safe_path, thumb = _prep(path)
                except Exception:
                    safe_path, thumb = path, None
                try:
                    sid, url = rc.submit_video(sub, title=title, video_path=safe_path, thumbnail_path=thumb)
                except Exception:
                    # Fallback: try without thumbnail
                    sid, url = rc.submit_video(sub, title=title, video_path=safe_path)
        if not sid:
            sid, url = rc.submit_text(sub, title=title, text=head or body)
        post.platform_post_id = sid
        # Prefer permalink to the post, not the direct media URL
        try:
            submission = rc.reddit.submission(id=sid)
            if submission and submission.permalink:
                post.platform_url = f"https://reddit.com{submission.permalink}"
            else:
                post.platform_url = url
        except Exception:
            post.platform_url = url
        # Mark done and return to avoid falling through into unrelated branches
        from app.models.models import PostStatusEnum, JobStatusEnum
        post.status = PostStatusEnum.done
        from datetime import datetime as _dt
        post.published_at = _dt.utcnow()
        if job is not None:
            job.status = JobStatusEnum.done
            session.add(job)
        session.add(post)
        session.commit()
        # If there is remaining text, schedule a comment thread with retries
        if rest_text:
            from app.jobs.tasks import reddit_reply_comment
            reddit_reply_comment.schedule(args=(post.platform_post_id, rest_text, 1), delay=30)
        try:
            from app.jobs.tasks import refresh_metrics_for_post
            refresh_metrics_for_post.schedule(args=(post.id,), delay=2)
        except Exception:
            pass
        return 'ok'
    except Exception as exc:
        post.status = PostStatusEnum.error
        post.error_text = str(exc)
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = str(exc)
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'


def fetch_metrics(post) -> dict:
    """Engagement counters for a published post; may raise (caller records the error)."""
    from app.services.settings import load_settings
    import httpx
    metrics: dict = {}
    sid = post.platform_post_id.split('_')[-1]
    cfg = load_settings()
    cid = (cfg.get('reddit_client_id') or '').strip()
    csec = (cfg.get('reddit_client_secret') or '').strip()
    uname = (cfg.get('reddit_username') or '').strip()
    pwd = (cfg.get('reddit_password') or '').strip()
    got = False
    if cid and csec and uname and pwd:
        # Authenticated read via PRAW. The public www.reddit.com/*.json endpoint
        # is rate-limited/blocked for datacenter IPs (was returning fetch_failed).
        try:
            from app.services.reddit_client import RedditClient
            rc = RedditClient(cid, csec, uname, pwd)
            sub = rc.reddit.submission(id=sid)
            metrics = {
                "like": int(sub.score),
                "comments": int(sub.num_comments),
                "upvote_ratio": sub.upvote_ratio,
            }
            got = True
        except Exception as exc:
            metrics = {"error": f"reddit_praw_failed: {exc}"}
    if not got:
        # Fallback: unauthenticated public JSON (works when not blocked)
        try:
            with httpx.Client(timeout=15, headers={"User-Agent": "IndiePulse/1.0 (analytics)"}) as client:
                r = client.get(f"https://www.reddit.com/comments/{sid}.json")
                arr = r.json()
            item = (arr[0].get('data', {}).get('children', [{}])[0].get('data', {})) if isinstance(arr, list) else {}
            if item:
                metrics = {"like": item.get('score'), "comments": item.get('num_comments')}
        except Exception:
            metrics = metrics or {"error": "reddit_fetch_failed"}
    return metrics
