"""Bluesky."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    # Bluesky via AT Protocol: text thread (300-char chunks) + up to 4 images
    from app.services.bluesky_client import BlueskyClient
    from app.services.settings import load_settings
    from app.models.models import Brief
    cfg = load_settings()
    handle = (cfg.get('bluesky_handle') or '').strip()
    app_pw = (cfg.get('bluesky_app_password') or '').strip()
    service = (cfg.get('bluesky_service') or 'https://bsky.social').strip()
    if not (handle and app_pw):
        post.status = PostStatusEnum.error
        post.error_text = 'bluesky_not_configured'
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = 'bluesky_not_configured'
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'
    brief = session.get(Brief, post.brief_id)
    base_text = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or '') if brief else ''
    hashtags = (getattr(brief, 'tags_json', None) or '').strip() if brief else ''
    langs = ['en'] if (brief and (getattr(brief, 'text_en', None) or '').strip()) else ['ru']
    # Collect image candidates: gallery first, then override, then brief media
    candidates: list[str] = []
    try:
        import json as _json
        if job and job.payload_json:
            parsed = (_json.loads(job.payload_json) or {})
            override = parsed.get('media_rel')
            gallery_list = parsed.get('gallery') or []
            if isinstance(gallery_list, list) and gallery_list:
                candidates = [str(x) for x in gallery_list if isinstance(x, str)]
            elif override:
                candidates = [str(override)]
    except Exception:
        candidates = []
    if not candidates:
        media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
        if media_rel:
            candidates = [media_rel]
    # Only images are embedded (video upload needs a separate service); text still posts
    import os as _os, mimetypes as _m
    image_paths: list[str] = []
    for rel in candidates[:4]:
        p = (rel or '').replace('\\', '/')
        if not p.startswith('media/'):
            p = f"media/{p}"
        abs_path = _os.path.abspath(p)
        if (_m.guess_type(abs_path)[0] or '').startswith('image/'):
            image_paths.append(abs_path)
    try:
        client = BlueskyClient(handle, app_pw, service)
        uri, web_url = client.post_thread(base_text, hashtags=hashtags, image_paths=image_paths, langs=langs)
        post.platform_post_id = uri
        post.platform_url = web_url
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
    metrics: dict = {}
    from app.services.bluesky_client import BlueskyClient
    metrics = BlueskyClient.get_post_metrics(post.platform_post_id) or {"error": "bluesky_fetch_failed"}
    return metrics
