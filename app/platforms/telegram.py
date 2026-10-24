"""Telegram channels (one per destination; a post without one goes to tg_chat_id_ru)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    import os
    try:
        from app.services.settings import load_settings
        from app.services.telegram_client import TelegramClient
        cfg = load_settings()
        bot = (cfg.get('tg_bot_token') or '').strip()
        from app.platforms.common import brief_for_publish, post_destination, post_text
        dest = post_destination(post, cfg)
        chat_id = ((dest['target'].get('chat_id') if dest else cfg.get('tg_chat_id_ru')) or '').strip()
        brief = brief_for_publish(session, post)
        if not (bot and chat_id and brief):
            raise RuntimeError('tg_not_configured')
        # No hashtags on Telegram — post the body text as-is, keeping its formatting.
        text = post_text(brief, post, ('ru', 'en'), cfg)
        client = TelegramClient(bot)
        # Prefer image/video if available (use normal/horizontal media)
        media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
        # Prefer override or first gallery
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
        sent = None
        if media_rel:
            import os as _os, mimetypes as _m
            p = media_rel.replace('\\','/')
            if not p.startswith('media/'):
                p = f"media/{p}"
            path = _os.path.abspath(p)
            ctype = _m.guess_type(path)[0] or ''
            if ctype.startswith('image/'):
                sent = client.send_photo(chat_id, path, caption=text)
            elif ctype.startswith('video/'):
                # Compress video if needed (Telegram limit 50MB)
                from app.services.video_editor import ensure_video_size_limit
                path = ensure_video_size_limit(path, 'tg', cfg)
                sent = client.send_video(chat_id, path, caption=text)
        if not sent:
            sent = client.send_message(chat_id, text)
        # Store message id
        try:
            mid = ((sent.get('result') or {}).get('message_id') if isinstance(sent, dict) else None)
            post.platform_post_id = str(mid) if mid else None
        except Exception:
            pass
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


def chat_of(post, cfg) -> str:
    """The chat a post went to: its destination's, or the original channel."""
    from app.platforms.common import post_destination
    try:
        dest = post_destination(post, cfg)
    except RuntimeError:
        return ''  # destination removed since: nothing to read
    return ((dest['target'].get('chat_id') if dest else cfg.get('tg_chat_id_ru')) or '').strip()


def fetch_metrics(post) -> dict:
    """Engagement counters for a published post; may raise (caller records the error)."""
    from app.services.settings import load_settings
    metrics: dict = {}
    # Bot API exposes no stats; for public channels we parse views from the t.me embed page
    cfg = load_settings()
    bot = (cfg.get('tg_bot_token') or '').strip()
    chat_id = chat_of(post, cfg)
    views = None
    try:
        if bot and chat_id:
            from app.services.telegram_client import TelegramClient
            views = TelegramClient(bot).get_channel_post_views(chat_id, int(post.platform_post_id))
    except Exception:
        views = None
    metrics = {"views": views} if views is not None else {"status": "sent"}
    return metrics
