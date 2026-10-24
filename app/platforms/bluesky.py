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
    from app.platforms.common import brief_for_publish, post_text_lang
    brief = brief_for_publish(session, post)
    base_text, text_lang = post_text_lang(brief, post, ('en', 'ru'), cfg)
    hashtags = (getattr(brief, 'tags_json', None) or '').strip() if brief else ''
    if getattr(post, 'destination_id', None):
        langs = [text_lang or 'en']  # the language the text really is in (after a fallback too)
    else:
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
    # One video (through Bluesky's video service) or up to 4 images. A post with media
    # never goes out as text only: any upload problem fails the post.
    import os as _os, mimetypes as _m
    abs_paths: list[str] = []
    for rel in candidates:
        p = (rel or '').replace('\\', '/')
        if not p.startswith('media/'):
            p = f"media/{p}"
        abs_paths.append(_os.path.abspath(p))
    video_path = next((p for p in abs_paths if (_m.guess_type(p)[0] or '').startswith('video/')), None)
    image_paths = [] if video_path else abs_paths[:4]
    try:
        if video_path:
            from app.services.video_editor import ensure_video_size_limit
            video_path = ensure_video_size_limit(video_path, 'bluesky', cfg)
        client = BlueskyClient(handle, app_pw, service)
        uri, web_url = client.post_thread(base_text, hashtags=hashtags, image_paths=image_paths, langs=langs,
                                          video_path=video_path)
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
