"""TikTok (inbox/draft upload; export fallback)."""
from __future__ import annotations

from typing import Optional


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
    brief = session.get(__import__('app.models.models', fromlist=['Brief']).Brief, post.brief_id)

    # Use vertical video
    media_rel = getattr(brief, 'media_vertical_path', None) or getattr(brief, 'media_path', None)

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
            title = (getattr(brief, 'title', None) or getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or 'New Video')[:150]

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
