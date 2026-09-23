"""Discord (webhooks; bot token only for reading reactions)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    from app.services.discord_client import DiscordClient
    from app.services.settings import load_settings
    import os
    settings = load_settings()
    # Compose per-language messages and target webhooks
    from app.models.models import Brief
    brief = session.get(Brief, post.brief_id)
    text_ru = (brief.text_ru or '').strip() if brief else ''
    text_en = (brief.text_en or '').strip() if brief else ''
    # Build gallery list for Discord; prefer payload.gallery, else single media (use normal/horizontal)
    gallery_files: list[str] = []
    primary_media: Optional[str] = None
    try:
        import json as _json
        override = None
        parsed = {}
        if job and job.payload_json:
            parsed = (_json.loads(job.payload_json) or {})
            override = parsed.get('media_rel')
        raw_gallery = parsed.get('gallery') if isinstance(parsed, dict) else None
        if isinstance(raw_gallery, list) and raw_gallery:
            gallery_files = [str(x) for x in raw_gallery if isinstance(x, str)]
        else:
            media_rel = override or (getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None) if brief else None)
            if media_rel:
                gallery_files = [media_rel]
        # Track first item as primary for video handling
        try:
            primary_media = (gallery_files[0] if gallery_files else None)
        except Exception:
            primary_media = None
    except Exception:
        media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
        if media_rel:
            gallery_files = [media_rel]
    # Normalize to absolute paths and compress videos if needed
    if gallery_files:
        import os as _os, mimetypes as _m
        from app.services.video_editor import ensure_video_size_limit
        _abs_files = []
        for rel in gallery_files:
            p = (rel or '').replace('\\', '/')
            if not p.startswith('media/'):
                p = f"media/{p}"
            abs_path = _os.path.abspath(p)
            # Compress videos if needed
            ctype = _m.guess_type(abs_path)[0] or ''
            if ctype.startswith('video/'):
                abs_path = ensure_video_size_limit(abs_path, 'discord', settings)
            _abs_files.append(abs_path)
        gallery_files = _abs_files
        try:
            if primary_media:
                pm = primary_media.replace('\\','/')
                if not pm.startswith('media/'):
                    pm = f"media/{pm}"
                primary_media = _os.path.abspath(pm)
                # Compress primary if video
                ctype = _m.guess_type(primary_media)[0] or ''
                if ctype.startswith('video/'):
                    primary_media = ensure_video_size_limit(primary_media, 'discord', settings)
        except Exception:
            primary_media = None

    targets = []
    if text_ru and settings.get('discord_webhook_ru'):
        targets.append(('ru', settings.get('discord_webhook_ru'), text_ru))
    if text_en and settings.get('discord_webhook_en'):
        targets.append(('en', settings.get('discord_webhook_en'), text_en))
    # Fallback: if no specific webhooks configured, use default once with whichever text available
    if not targets and settings.get('discord_webhook_url'):
        fallback_text = text_en or text_ru or '[IndiePulse] New post'
        targets.append(('default', settings.get('discord_webhook_url'), fallback_text))

    # Per-target progress lives in the job payload, so a retry (auto or manual)
    # only sends to webhooks that haven't received the post yet.
    import json as _json
    import mimetypes as _m
    from app.services.discord_client import DiscordSendError
    payload = {}
    if job is not None and job.payload_json:
        try:
            payload = _json.loads(job.payload_json) or {}
        except Exception:
            payload = {}
    sent: dict = dict(payload.get('discord_sent') or {})

    def _save_progress() -> None:
        if job is not None:
            payload['discord_sent'] = sent
            job.payload_json = _json.dumps(payload, ensure_ascii=False)
            session.add(job)
            session.commit()

    chosen_video = None
    if primary_media and (_m.guess_type(primary_media)[0] or '').startswith('video/'):
        chosen_video = primary_media
    else:
        chosen_video = next((fp for fp in gallery_files if (_m.guess_type(fp)[0] or '').startswith('video/')), None)

    last_url = None
    for key, wh_url, content in targets:
        if key in sent:
            continue
        client = DiscordClient(wh_url)
        try:
            try:
                if chosen_video:
                    _, sent_url = client.send_message(content, file_path=chosen_video)
                elif gallery_files:
                    _, sent_url = client.send_message(content, file_paths=gallery_files)
                else:
                    _, sent_url = client.send_message(content)
            except DiscordSendError as exc:
                # Fall back to text-only ONLY if Discord definitely rejected the upload
                # (e.g. file too large). After a timeout it may have landed already.
                if exc.delivered is False and (chosen_video or gallery_files):
                    _, sent_url = client.send_message(content + "\n[Attachment upload failed]")
                else:
                    raise
        except DiscordSendError as exc:
            if exc.delivered is not False:
                # possibly/partly posted: never resend to this webhook automatically
                sent[key] = '?'
                _save_progress()
            post.status = PostStatusEnum.error
            post.error_text = f"discord[{key}]: {exc}" + (" (may have been posted — check the channel)" if exc.delivered is None else "")
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            return 'error'
        except Exception as exc:
            # local failure before anything was sent (bad file, empty URL...)
            post.status = PostStatusEnum.error
            post.error_text = f"discord[{key}]: {type(exc).__name__}: {exc}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            return 'error'
        last_url = sent_url or last_url
        # channel_id/message_id let analytics read reactions via the bot REST API
        # (webhook responses usually omit guild_id, so no URL is built).
        ch_id = getattr(client, 'last_channel_id', None)
        msg_id = getattr(client, 'last_message_id', None)
        sent[key] = f"{ch_id}/{msg_id}" if (ch_id and msg_id) else '?'
        _save_progress()
    post.platform_url = last_url or post.platform_url
    refs = [v for v in sent.values() if v and v != '?']
    if refs:
        post.platform_post_id = ','.join(refs)


def fetch_metrics(post) -> dict:
    """Engagement counters for a published post; may raise (caller records the error)."""
    from app.services.settings import load_settings
    metrics: dict = {}
    cfg = load_settings()
    bot_token = (cfg.get('discord_bot_token') or '').strip()
    # Message refs are stored as "channel_id/message_id" (comma-separated for
    # multi-target posts). Older posts predate that, so fall back to parsing
    # the channel/message out of the stored Discord message URL.
    refs = (post.platform_post_id or '').strip()
    if not refs and post.platform_url and '/channels/' in (post.platform_url or ''):
        try:
            parts = post.platform_url.rstrip('/').split('/')
            ch_id, msg_id = parts[-2], parts[-1]
            if ch_id.isdigit() and msg_id.isdigit():
                refs = f"{ch_id}/{msg_id}"
        except Exception:
            refs = ''
    if not bot_token:
        metrics = {"status": "sent", "note": "discord_bot_token_not_set"}
    elif not refs:
        metrics = {"status": "sent", "note": "no_message_id"}
    else:
        from app.services.discord_client import DiscordClient
        total_like = 0
        breakdown: dict = {}
        errors = []
        for ref in refs.split(','):
            ref = ref.strip()
            if '/' not in ref:
                continue
            ch_id, msg_id = ref.split('/', 1)
            try:
                m = DiscordClient.get_message_reactions(bot_token, ch_id, msg_id)
                total_like += int(m.get('like') or 0)
                for k, v in (m.get('reactions') or {}).items():
                    breakdown[k] = breakdown.get(k, 0) + int(v or 0)
            except Exception as exc:
                errors.append(str(exc))
        if errors and total_like == 0 and not breakdown:
            metrics = {"error": f"discord: {errors[0]}"}
        else:
            metrics = {"like": total_like, "reactions": breakdown}
    return metrics
