"""TikTok (inbox/draft upload; export fallback)."""
from __future__ import annotations

from typing import Optional

VIDEO_FIELDS = ["id", "create_time", "share_url", "view_count", "like_count", "comment_count", "share_count"]


def metrics_of(video: dict) -> dict:
    """A /video/query/ video → metrics_json keys (shares count as reposts, like X's)."""
    def num(key):
        v = video.get(key)
        return int(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None
    return {"views": num("view_count"), "likes": num("like_count"),
            "comments": num("comment_count"), "reposts": num("share_count")}


def fetch_metrics(post) -> dict:
    """Counters of a live TikTok post. Needs a real video id (an inbox draft only has a
    publish id) and the video.list scope; without them there is nothing to read."""
    from app.services.settings import load_settings
    from app.services.tiktok_client import TikTokClient, get_valid_access_token
    vid = str(post.platform_post_id or "").strip()
    cfg = load_settings()
    if not vid.isdigit() or "video.list" not in str(cfg.get("tiktok_scope") or ""):
        return {}
    token = get_valid_access_token(cfg)
    if not token:
        return {"error": "tiktok_not_connected"}
    videos = TikTokClient(token).query_videos([vid], VIDEO_FIELDS)
    return metrics_of(videos[0]) if videos else {"error": "tiktok_video_not_found"}


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    from app.services.settings import load_settings
    from app.services.tiktok_client import get_valid_access_token
    cfg = load_settings()
    # Auto-refreshes the 24h TikTok access token via the refresh token
    access_token = get_valid_access_token(cfg)
    from app.platforms.common import brief_for_publish
    brief = brief_for_publish(session, post)

    # The job's file first: the /tiktok pipeline sends its own version (no baked music);
    # the factory and cross-posts pass the brief's vertical render.
    override = None
    if job is not None and job.payload_json:
        try:
            import json as _json
            override = (_json.loads(job.payload_json) or {}).get('media_rel')
        except Exception:
            override = None
    media_rel = override or getattr(brief, 'media_vertical_path', None) or getattr(brief, 'media_path', None)

    if not media_rel:
        post.status = PostStatusEnum.error
        post.error_text = 'tiktok_no_media'
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = 'tiktok_no_media'
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'

    import os as _os
    p = media_rel.replace('\\','/')
    if not p.startswith('media/'):
        p = f"media/{p}"
    video_path = _os.path.abspath(p)

    # Compress video if needed (TikTok limit ~287MB)
    from app.services.video_editor import ensure_video_size_limit
    video_path = ensure_video_size_limit(video_path, 'tiktok', cfg)

    if access_token:
        # Use TikTok API
        try:
            from app.services.tiktok_client import TikTokClient
            from app.platforms.common import post_text
            title = (getattr(brief, 'title', None) or post_text(brief, post, ('en', 'ru'), cfg) or 'New Video')[:150]

            client = TikTokClient(access_token)
            # Draft/inbox upload: the clip lands in the creator's TikTok
            # inbox. They open the app, add a native/trending sound, write
            # the caption and publish in one tap. (Public Direct Post needs
            # a passed app audit — see TikTokClient.direct_post_public.)
            publish_id, _share = client.upload_to_inbox(video_path, title=title)

            post.platform_post_id = publish_id
            post.platform_url = None
            # Not "done": it sits in the inbox awaiting the manual finish.
            post.status = PostStatusEnum.awaiting_finish
            if job is not None:
                job.status = JobStatusEnum.done
                session.add(job)
            session.add(post)
            session.commit()
            return 'ok'
        except Exception as exc:
            post.status = PostStatusEnum.error
            post.error_text = f"tiktok_api_failed: {exc}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
            session.add(post)
            if job is not None:
                session.add(job)
            session.commit()
            return 'error'
    else:
        # Fallback: export to directory for manual upload
        import shutil as _sh
        export_dir = (cfg.get('tiktok_export_dir') or 'render').strip() or 'render'
        _os.makedirs(export_dir, exist_ok=True)
        dst = _os.path.join(export_dir, _os.path.basename(video_path))
        try:
            _sh.copy2(video_path, dst)
            post.platform_url = None
            # Exported to a folder for manual upload — same "awaiting the
            # user's manual finish" state as the inbox flow.
            post.status = PostStatusEnum.awaiting_finish
            if job is not None:
                job.status = JobStatusEnum.done
                session.add(job)
            session.add(post)
            session.commit()
            return 'ok'
        except Exception as exc:
            post.status = PostStatusEnum.error
            post.error_text = f"tiktok_export_failed: {exc}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
            session.add(post)
            if job is not None:
                session.add(job)
            session.commit()
            return 'error'
