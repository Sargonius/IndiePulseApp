"""Instagram Reels (Graph API resumable upload; export fallback)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    # Instagram Reels via Graph API resumable upload; export fallback if unconfigured.
    from app.services.settings import load_settings
    from app.models.models import Brief
    cfg = load_settings()
    ig_user_id = (cfg.get('ig_user_id') or '').strip()
    ig_token = (cfg.get('ig_access_token') or '').strip()
    from app.platforms.common import brief_for_publish
    brief = brief_for_publish(session, post)
    media_rel = getattr(brief, 'media_vertical_path', None) or getattr(brief, 'media_path', None)
    if not media_rel:
        post.status = PostStatusEnum.error
        post.error_text = 'ig_no_media'
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = 'ig_no_media'
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'
    import os as _os
    p = media_rel.replace('\\', '/')
    if not p.startswith('media/'):
        p = f"media/{p}"
    video_path = _os.path.abspath(p)
    from app.platforms.common import post_text
    caption = (post_text(brief, post, ('en', 'ru'), cfg) or getattr(brief, 'title', None) or '')
    if brief and getattr(brief, 'tags_json', None):
        try:
            caption = (caption + '\n' + brief.tags_json).strip()
        except Exception:
            pass
    if ig_user_id and ig_token:
        try:
            from app.services.instagram_client import InstagramClient
            ig = InstagramClient(ig_user_id, ig_token)
            media_id, permalink = ig.upload_reel(video_path, caption=caption, share_to_feed=True)
            post.platform_post_id = media_id
            post.platform_url = permalink
        except Exception as exc:
            post.status = PostStatusEnum.error
            post.error_text = f"ig_reels_failed: {exc}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
            session.add(post)
            if job is not None:
                session.add(job)
            session.commit()
            return 'error'
    else:
        # Not configured / not yet approved by Meta: export for manual upload.
        import shutil as _sh
        export_dir = (cfg.get('tiktok_export_dir') or 'render').strip() or 'render'
        _os.makedirs(export_dir, exist_ok=True)
        try:
            dst = _os.path.join(export_dir, _os.path.basename(video_path))
            _sh.copy2(video_path, dst)
            with open(_os.path.splitext(dst)[0] + '_caption.txt', 'w', encoding='utf-8') as _cf:
                _cf.write(caption or '')
            post.platform_url = None
            post.status = PostStatusEnum.awaiting_finish
            if job is not None:
                job.status = JobStatusEnum.done
                session.add(job)
            session.add(post)
            session.commit()
            return 'ok'
        except Exception as exc:
            post.status = PostStatusEnum.error
            post.error_text = f"ig_export_failed: {exc}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
            session.add(post)
            if job is not None:
                session.add(job)
            session.commit()
            return 'error'
