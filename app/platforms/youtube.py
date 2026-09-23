"""YouTube uploads."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    # YouTube upload - use vertical for Shorts if available, otherwise normal
    from app.services.youtube_client import YouTubeClient
    from app.services.settings import load_settings
    from loguru import logger as _logger
    cfg = load_settings()
    brief = session.get(__import__('app.models.models', fromlist=['Brief']).Brief, post.brief_id)
    media_rel = None
    try:
        import json as _json
        override = None
        if job and job.payload_json:
            try:
                override = (_json.loads(job.payload_json) or {}).get('media_rel')
            except Exception:
                override = None
        # Try vertical first (for Shorts), then normal, then generic
        media_rel = override or getattr(brief, 'media_vertical_path', None) or getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
    except Exception:
        media_rel = getattr(brief, 'media_path', None)
    if not media_rel:
        post.status = PostStatusEnum.error
        post.error_text = 'yt_no_media'
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = 'yt_no_media'
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'
    path = media_rel.replace('\\', '/')
    if not path.startswith('media/'):
        path = f"media/{path}"
    import os as _os
    path = _os.path.abspath(path)
    # Title preference: use explicit yt_title from job/brief if present, else EN then RU
    title = None
    try:
        import json as _json
        override_title = None
        if job and job.payload_json:
            try:
                override_title = (_json.loads(job.payload_json) or {}).get('yt_title')
            except Exception:
                override_title = None
        if override_title:
            title = override_title
    except Exception:
        pass
    if not title:
        title = (getattr(brief, 'title', None) or getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or 'IndiePulse Short')[:90]
    # For YouTube, include English description and hashtags
    desc = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or 'Uploaded via IndiePulse')
    if brief and getattr(brief, 'tags_json', None):
        try:
            desc = (desc + '\n' + brief.tags_json).strip()
        except Exception:
            pass
    # Per-post options from the Create Video form (absent for Shorts Factory jobs)
    yt_opts = {}
    try:
        import json as _json
        if job and job.payload_json:
            yt_opts = _json.loads(job.payload_json) or {}
    except Exception:
        yt_opts = {}
    privacy = (yt_opts.get('yt_privacy') or 'public').strip().lower()
    if privacy not in ('public', 'unlisted', 'private'):
        privacy = 'public'
    try:
        from app.services.tokens import yt_access_token
        access = yt_access_token()
        yt = YouTubeClient(
            access_token=access,
            refresh_token=(cfg.get('yt_refresh_token') or '').strip(),
            client_id=(cfg.get('yt_client_id') or '').strip(),
            client_secret=(cfg.get('yt_client_secret') or '').strip(),
        )
        _logger.info(f"yt.upload start path={path} title_len={len(title or '')} desc_len={len(desc or '')} privacy={privacy}")
        vid, url = yt.upload_video(path, title=title, description=desc, privacy_status=privacy)
        post.platform_post_id = vid
        post.platform_url = url
        # Playlist: the form's choice wins ('' = "No playlist"); otherwise the settings default
        if 'yt_playlist' in yt_opts:
            pname = (yt_opts.get('yt_playlist') or '').strip()
            playlist_id = (yt.ensure_playlist(pname[:1].upper() + pname[1:]) or '') if pname else ''
        else:
            playlist_id = (cfg.get('yt_playlist_id') or '').strip()
            if not playlist_id:
                pname = (cfg.get('yt_playlist_title') or 'Synthetica').strip()
                playlist_id = yt.ensure_playlist(pname) or ''
        if playlist_id and vid:
            try:
                yt.add_to_playlist(playlist_id, vid)
                _logger.info(f"yt.add_to_playlist ok playlist_id={playlist_id} video_id={vid}")
            except Exception:
                pass
    except Exception as exc:
        _logger.error(f"yt.upload error: {exc}")
        post.status = PostStatusEnum.error
        err_text = str(exc)
        if 'invalid_grant' in err_text:
            err_text = ("yt_reauth_needed: refresh token протух/отозван — "
                        "нажми Connect YouTube в настройках аккаунтов. " + err_text)
        post.error_text = err_text
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = err_text
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'


def fetch_metrics(post) -> dict:
    """Engagement counters for a published post; may raise (caller records the error)."""
    import httpx
    metrics: dict = {}
    # YouTube stats: requires access token
    from app.services.tokens import yt_access_token
    try:
        access = yt_access_token()
    except Exception as exc:
        access = ''
        metrics = {"error": str(exc)}
    if access:
        with httpx.Client(timeout=15) as client:
            r = client.get("https://www.googleapis.com/youtube/v3/videos", params={
                "part": "statistics",
                "id": post.platform_post_id,
            }, headers={"Authorization": f"Bearer {access}"})
            data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"yt_metrics status={r.status_code} body={str(data)[:200]}")
        stats = ((data.get('items') or [{}])[0].get('statistics') or {})
        likes = stats.get('likeCount')
        comments = stats.get('commentCount')
        views = stats.get('viewCount')
        metrics = {"likes": int(likes) if likes is not None else None,
                   "comments": int(comments) if comments is not None else None,
                   "views": int(views) if views is not None else None}
    return metrics
