"""Facebook Pages (RU / EN)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    # Facebook Page posting (separate RU / EN pages)
    from app.services.facebook_client import FacebookClient
    from app.services.settings import load_settings
    from app.models.models import Brief
    cfg = load_settings()
    from app.platforms.common import brief_for_publish
    brief = brief_for_publish(session, post)
    is_ru = str(post.platform).endswith('fb_ru') or str(post.platform) == 'fb_ru'
    page_id = (cfg.get('fb_ru_page_id') if is_ru else cfg.get('fb_en_page_id')) or ''
    page_token = (cfg.get('fb_ru_page_access_token') if is_ru else cfg.get('fb_en_page_access_token')) or ''
    if not page_id or not page_token:
        post.status = PostStatusEnum.error
        post.error_text = 'fb_missing_page_or_token'
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = 'fb_missing_page_or_token'
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'
    # RU page gets RU text, EN page gets EN text (with fallback to the other)
    primary = ((getattr(brief, 'text_ru', None) if is_ru else getattr(brief, 'text_en', None)) or '') if brief else ''
    fallback = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or '') if brief else ''
    message = (primary or fallback).strip()
    if brief and getattr(brief, 'tags_json', None):
        try:
            message = (message + '\n' + brief.tags_json).strip()
        except Exception:
            pass
    # Prefer override / first gallery item, then normal/horizontal media
    media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
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
    path_abs = None
    if media_rel:
        p = media_rel.replace('\\', '/')
        if not p.startswith('media/'):
            p = f"media/{p}"
        import os as _os
        path_abs = _os.path.abspath(p)
    try:
        from loguru import logger as _logger
        import mimetypes as _m
        fb = FacebookClient(page_token)
        _logger.info(f"fb.publish start platform={'fb_ru' if is_ru else 'fb_en'} page_id={page_id}")
        ctype = (_m.guess_type(path_abs)[0] or '') if path_abs else ''
        # A post with media never goes out as text only: media problems fail the post.
        if path_abs and ctype.startswith('image/'):
            _logger.info(f"fb.publish create_photo path={path_abs} msg_len={len(message or '')}")
            resp = fb.create_photo(page_id, path_abs, caption=message)
            fb_post_id = str(resp.get('post_id') or resp.get('id') or '')
        elif path_abs and ctype.startswith('video/'):
            from app.services.video_editor import ensure_video_size_limit
            path_abs = ensure_video_size_limit(path_abs, 'fb_ru' if is_ru else 'fb_en', cfg)
            resp = fb.create_video(page_id, path_abs, description=message)
            video_id = str(resp.get('id') or '')
            if not video_id:
                raise RuntimeError(f"fb video upload returned no id: {str(resp)[:200]}")
            fb_post_id = fb.video_post_id(page_id, video_id)
        elif path_abs:
            raise RuntimeError(f"fb: unsupported media type for {os.path.basename(path_abs)}")
        else:
            _logger.info(f"fb.publish create_post msg_len={len(message or '')}")
            resp = fb.create_post(page_id, message)
            fb_post_id = str(resp.get('post_id') or resp.get('id') or '')
        _logger.info(f"fb.publish resp id={fb_post_id} raw={str(resp)[:400]}")
        post.platform_post_id = fb_post_id
        try:
            post.platform_url = fb.get_permalink(fb_post_id)
            _logger.info(f"fb.publish permalink={post.platform_url}")
        except Exception as _exc:
            _logger.warning(f"fb.publish permalink error: {_exc}")
            post.platform_url = None
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
    platform = str(post.platform)
    metrics: dict = {}
    # Facebook Page post insights: use summary counts for reactions and comments; shares via 'shares' field
    cfg = load_settings()
    is_ru = platform.endswith('fb_ru') or platform == 'fb_ru'
    page_token = (cfg.get('fb_ru_page_access_token') if is_ru else cfg.get('fb_en_page_access_token')) or ''
    if not (post.platform_post_id and page_token):
        metrics = {"error": "fb_not_configured"}
    else:
        with httpx.Client(timeout=20) as client:
            fields = "reactions.summary(true),comments.summary(true),shares"
            r = client.get(f"https://graph.facebook.com/v19.0/{post.platform_post_id}", params={"fields": fields, "access_token": page_token})
            d = r.json()
            if 'error' in d and '_' not in post.platform_post_id:
                # a bare video id (its Page post id wasn't exposed): videos have no 'shares'
                r = client.get(f"https://graph.facebook.com/v19.0/{post.platform_post_id}",
                               params={"fields": "reactions.summary(true),comments.summary(true)",
                                       "access_token": page_token})
                d = r.json()
        if 'error' in d:
            raise RuntimeError(f"fb_metrics: {(d.get('error') or {}).get('message')}")
        likes = (((d.get('reactions') or {}).get('summary') or {}).get('total_count'))
        comments = (((d.get('comments') or {}).get('summary') or {}).get('total_count'))
        shares = ((d.get('shares') or {}).get('count'))
        metrics = {"likes": likes, "comments": comments, "reposts": shares}
        # Best-effort reach via insights (needs pages_read_engagement)
        try:
            with httpx.Client(timeout=15) as client:
                r2 = client.get(
                    f"https://graph.facebook.com/v19.0/{post.platform_post_id}/insights",
                    params={"metric": "post_impressions", "access_token": page_token},
                )
                d2 = r2.json()
            vals = ((d2.get('data') or [{}])[0].get('values') or [{}])
            impressions = vals[0].get('value')
            if isinstance(impressions, int):
                metrics["views"] = impressions
        except Exception:
            pass
    return metrics
