from datetime import datetime
from typing import Optional
from .huey_app import huey
from sqlmodel import Session
from app.db.session import engine
from huey import crontab


@huey.task()
def publish_news_post(job_id: int, post_id: int) -> str:
    """Publish one post to one platform; auto-retry ONCE on transient failure."""
    result = _publish_news_post_impl(job_id, post_id)
    try:
        _maybe_retry_publish(job_id, post_id, result)
    except Exception:
        pass
    return result


def _maybe_retry_publish(job_id: int, post_id: int, result: str) -> None:
    """Requeue a failed publish one time. Guards against duplicates:
    - skips if the post already has a platform_post_id (publish reached the
      platform, so re-running would double-post);
    - skips if this job already spent its single retry (tracked in the payload's
      'publish_attempt').
    Covers the common VK case where a connect/timeout blip fails the first try
    but a manual re-run succeeds — now that re-run happens automatically once.
    """
    import json as _json
    from app.models.models import Post, PostStatusEnum, Job, JobStatusEnum
    with Session(engine) as session:
        post = session.get(Post, post_id)
        job = session.get(Job, job_id)
        if post is None or post.status != PostStatusEnum.error:
            return
        if getattr(post, 'platform_post_id', None):
            return  # already landed on the platform — don't double-post
        payload = {}
        if job is not None and job.payload_json:
            try:
                payload = _json.loads(job.payload_json) or {}
            except Exception:
                payload = {}
        if int(payload.get('publish_attempt') or 0) >= 1:
            return  # one retry only
        payload['publish_attempt'] = 1
        post.status = PostStatusEnum.queued
        session.add(post)
        if job is not None:
            job.status = JobStatusEnum.queued
            job.payload_json = _json.dumps(payload, ensure_ascii=False)
            job.last_error = ((job.last_error or '') + ' | auto-retry #1').strip()
            session.add(job)
        session.commit()
    from app.jobs.tasks import publish_news_post as _pub
    _pub.schedule(args=(job_id, post_id), delay=20)


def _publish_news_post_impl(job_id: int, post_id: int) -> str:
    from app.models.models import Post, PostStatusEnum, Job, JobStatusEnum
    from app.services.discord_client import DiscordClient
    from app.services.settings import load_settings
    import os
    with Session(engine) as session:
        job = session.get(Job, job_id)
        post = session.get(Post, post_id)
        if post is None:
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = "post_not_found"
                session.add(job)
                session.commit()
            return "not_found"
        # Skip if not queued anymore (avoid duplicate publish)
        if post.status not in (PostStatusEnum.queued, PostStatusEnum.running):
            # Close the job too, otherwise it hangs in 'queued' forever
            if job is not None:
                job.status = JobStatusEnum.done
                job.last_error = ((job.last_error or '') + ' skipped_already_handled').strip()
                session.add(job)
                session.commit()
            return 'skipped_already_handled'

        # Respect schedule edits: if scheduled_at is in the future, reschedule and exit
        try:
            if getattr(post, 'scheduled_at', None) is not None:
                # Compare using local naive time, because scheduled_at comes from the browser in local time
                from datetime import datetime as _dt
                now = _dt.now()
                target = post.scheduled_at
                if getattr(target, 'tzinfo', None) is not None:
                    try:
                        target = target.replace(tzinfo=None)
                    except Exception:
                        pass
                if target and target > now:
                    # Requeue this job for that future time
                    from app.jobs.tasks import publish_news_post as _pub
                    delay_s = int((target - now).total_seconds())
                    _pub.schedule(args=(job_id, post_id), delay=max(1, delay_s))
                    if job is not None:
                        job.status = JobStatusEnum.queued
                        session.add(job)
                    session.commit()
                    return 'rescheduled'
        except Exception:
            pass

        # mark running
        post.status = PostStatusEnum.running
        if job is not None:
            job.status = JobStatusEnum.running
            session.add(job)
        session.add(post)
        session.commit()
        # Simulate publish work; if discord, try sending
        if str(post.platform) == 'PlatformEnum.discord' or str(post.platform) == 'discord':
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
                targets.append((settings.get('discord_webhook_ru'), text_ru))
            if text_en and settings.get('discord_webhook_en'):
                targets.append((settings.get('discord_webhook_en'), text_en))
            # Fallback: if no specific webhooks configured, use default once with whichever text available
            if not targets and settings.get('discord_webhook_url'):
                fallback_text = text_en or text_ru or '[IndiePulse] New post'
                targets.append((settings.get('discord_webhook_url'), fallback_text))

            last_url = None
            msg_refs: list[str] = []
            for wh_url, content in targets:
                try:
                    client = DiscordClient(wh_url)
                    # Prefer single primary video if available; else send images/generic
                    sent_url = None
                    try:
                        import mimetypes as _m
                        # If there is any video in gallery, prefer the first video file
                        chosen_video = None
                        if primary_media and (_m.guess_type(primary_media)[0] or '').startswith('video/'):
                            chosen_video = primary_media
                        else:
                            for fp in gallery_files:
                                if (_m.guess_type(fp)[0] or '').startswith('video/'):
                                    chosen_video = fp
                                    break
                        if chosen_video:
                            _, sent_url = client.send_message(content, file_path=chosen_video)
                        elif gallery_files:
                            _, sent_url = client.send_message(content, file_paths=gallery_files)
                        else:
                            _, sent_url = client.send_message(content)
                    except Exception:
                        _, sent_url = client.send_message(content)
                    murl = sent_url
                    last_url = murl or last_url
                    # Capture channel_id/message_id so we can read reactions later via the
                    # bot REST API (webhooks can't read them back). Taken from the client,
                    # since webhook responses usually omit guild_id and build no URL.
                    try:
                        ch_id = getattr(client, 'last_channel_id', None)
                        msg_id = getattr(client, 'last_message_id', None)
                        if ch_id and msg_id:
                            msg_refs.append(f"{ch_id}/{msg_id}")
                    except Exception:
                        pass
                except Exception as exc:
                    # continue to next target but record error on post/job
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
            post.platform_url = last_url
            if msg_refs:
                post.platform_post_id = ','.join(msg_refs)
        elif str(post.platform) == 'PlatformEnum.x' or str(post.platform) == 'x':
            from app.services.x_client import XClient
            from app.services.x_media import XMediaUploader
            cfg = load_settings()
            # Ensure/refresh token
            try:
                from app.routers.api import _x_ensure_token
                token = _x_ensure_token()
            except Exception:
                token = (cfg.get('x_access_token') or '').strip()
            if not token:
                post.status = PostStatusEnum.error
                if job is not None:
                    job.status = JobStatusEnum.error
                    job.last_error = 'x_not_connected'
                session.add(post)
                if job is not None:
                    session.add(job)
                session.commit()
                return 'error'
            # Compose text from brief if available
            from app.models.models import Brief
            brief = session.get(Brief, post.brief_id)
            base_text = (brief.text_en or brief.text_ru or '') if brief else ''
            hashtags = ''
            if brief and getattr(brief, 'tags_json', None):
                try:
                    hashtags = (brief.tags_json or '').strip()
                except Exception:
                    hashtags = ''
            media_ids: list[str] = []
            # Use normal/horizontal video/image for X (Twitter)
            media_rel: Optional[str] = None
            try:
                # Helper to build first tweet that always includes hashtags, trimming base text first
                def _compose_main_and_remaining(text_body: str, tag_str: str) -> tuple[str, str]:
                    body = (text_body or '').strip()
                    tags = (tag_str or '').strip()
                    if not tags:
                        if len(body) <= 280:
                            return body, ''
                        # trim body to 280 on word boundary
                        head = body[:280]
                        cut = head.rfind(' ')
                        if cut > 40:
                            head = head[:cut]
                        tail = body[len(head):].lstrip()
                        return head, tail
                    # ensure hashtags stay in the first tweet; trim body if needed
                    fixed_suffix = ('\n' + tags)
                    budget = 280 - len(fixed_suffix)
                    if budget <= 0:
                        # Keep full hashtags (if they exceed 280, API will reject and caller will handle)
                        return fixed_suffix, body  # push entire body to replies
                    if len(body) <= budget:
                        return (body + fixed_suffix), ''
                    # need to trim body to fit
                    head = body[:budget]
                    cut = head.rfind(' ')
                    if cut > 40:
                        head = head[:cut]
                    remaining = body[len(head):].lstrip()
                    return (head + fixed_suffix), remaining
                import json as _json
                override = None
                if job and job.payload_json:
                    try:
                        parsed = (_json.loads(job.payload_json) or {})
                        override = parsed.get('media_rel')
                        gallery_list = parsed.get('gallery') or []
                    except Exception:
                        override = None
                        gallery_list = []
                # Prefer normal/horizontal media
                media_rel = override or getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
            except Exception:
                media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
            # If gallery present, upload up to 4 images (X limit); else single media if present
            gallery_media: list[str] = []
            try:
                if job and job.payload_json:
                    import json as _json2
                    parsed = (_json2.loads(job.payload_json) or {})
                    if isinstance(parsed.get('gallery'), list):
                        gallery_media = [str(x) for x in parsed.get('gallery') if isinstance(x, str)]
            except Exception:
                gallery_media = []
            if gallery_media:
                api_key = (cfg.get('x_api_key') or '').strip()
                api_secret = (cfg.get('x_api_secret') or '').strip()
                oauth_token = (cfg.get('x_oauth_token') or '').strip()
                oauth_secret = (cfg.get('x_oauth_token_secret') or '').strip()
                if api_key and api_secret and oauth_token and oauth_secret:
                    try:
                        uploader = XMediaUploader(oauth_token, oauth_secret, api_key, api_secret)
                        for rel in gallery_media[:4]:
                            path = rel.replace('\\', '/')
                            if not path.startswith('media/'):
                                path = f"media/{path}"
                            import os as _os
                            mid = uploader.upload(_os.path.abspath(path))
                            media_ids.append(mid)
                    except Exception as exc:
                        if job is not None:
                            job.last_error = f"x_gallery_upload_failed: {exc}"
                            session.add(job)
                            session.commit()
            elif media_rel:
                # Use OAuth1 for media upload (requires keys/tokens)
                api_key = (cfg.get('x_api_key') or '').strip()
                api_secret = (cfg.get('x_api_secret') or '').strip()
                oauth_token = (cfg.get('x_oauth_token') or '').strip()
                oauth_secret = (cfg.get('x_oauth_token_secret') or '').strip()
                if api_key and api_secret and oauth_token and oauth_secret:
                    try:
                        uploader = XMediaUploader(oauth_token, oauth_secret, api_key, api_secret)
                        path = media_rel
                        if path:
                            path = path.replace('\\', '/')
                            if not path.startswith('media/'):
                                path = f"media/{path}"
                            import os as _os
                            path = _os.path.abspath(path)
                        media_ids.append(uploader.upload(path))
                    except Exception as exc:
                        # if media upload fails, fallback to text-only
                        if job is not None:
                            job.last_error = f"x_media_upload_failed: {exc}"
                            session.add(job)
                            session.commit()
                else:
                    # OAuth1 not configured
                    if job is not None:
                        job.last_error = "x_media_upload_skipped: oauth1_missing"
                        session.add(job)
                        session.commit()
            client = XClient(token)
            import httpx as _httpx
            from app.services.bluesky_client import _sentence_chunks as _x_chunks
            _x_headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

            def _x_post_v2(text_val: str, media: Optional[list] = None, reply_to: Optional[str] = None) -> Optional[str]:
                payload = {"text": text_val}
                if media:
                    payload["media"] = {"media_ids": media[:4]}
                if reply_to:
                    payload["reply"] = {"in_reply_to_tweet_id": reply_to}
                with _httpx.Client(timeout=20) as _h:
                    r = _h.post("https://api.twitter.com/2/tweets", json=payload, headers=_x_headers)
                    d = r.json()
                if r.status_code >= 400:
                    raise RuntimeError(f"x_tweet_error status={r.status_code} body={d}")
                return (d.get('data') or {}).get('id')

            def _x_thread_fallback(media: Optional[list] = None) -> Optional[str]:
                """280-safe thread: first tweet keeps hashtags (+media), the rest go as replies.
                Used when the account has no Premium and a long single tweet got 403."""
                tags = (hashtags or '').strip()
                fixed = ('\n' + tags) if tags else ''
                budget = max(40, 280 - len(fixed))
                chunks = _x_chunks((base_text or '').strip(), budget)
                first_text = ((chunks[0] if chunks else '') + fixed).strip() or tags
                first_id = _x_post_v2(first_text, media=media)
                parent = first_id
                for seg in chunks[1:]:
                    if not parent:
                        break
                    parent = _x_post_v2(seg, reply_to=parent)
                return first_id
            try:
                # Read x_thread option from job payload (default False)
                thread_enabled = False
                try:
                    import json as _json
                    if job and job.payload_json:
                        val = (_json.loads(job.payload_json) or {}).get('x_thread')
                        thread_enabled = True if val is True else False
                except Exception:
                    thread_enabled = False
                if media_ids:
                    # Use v2 create tweet with media
                    import httpx, re
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    if not thread_enabled:
                        # Send full text in one tweet (works with Premium, >280 allowed).
                        # Without Premium X returns 403 — fall back to a thread automatically.
                        full_text = (base_text or '').strip()
                        if hashtags:
                            full_text = (full_text + '\n' + hashtags).strip()
                        try:
                            tid = _x_post_v2(full_text, media=media_ids)
                        except RuntimeError as exc:
                            if len(full_text) > 280 and 'status=403' in str(exc):
                                tid = _x_thread_fallback(media=media_ids)
                                if job is not None:
                                    job.last_error = ((job.last_error or '') + ' x_280_limit_fallback_thread').strip()
                                    session.add(job)
                                    session.commit()
                            else:
                                raise
                        url = f"https://twitter.com/i/web/status/{tid}" if tid else None
                    else:
                        # Split by sentence boundaries; ensure hashtags in first tweet
                        def _sentence_chunks(text: str, limit: int) -> list[str]:
                            parts = re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", text)
                            chunks = []
                            buf = ''
                            for s in parts:
                                s = s.strip()
                                if not s:
                                    continue
                                if not buf:
                                    buf = s
                                elif len(buf) + 1 + len(s) <= limit:
                                    buf += ' ' + s
                                else:
                                    chunks.append(buf)
                                    buf = s
                            if buf:
                                chunks.append(buf)
                            return chunks
                        # Budget for first tweet includes hashtags
                        fixed = ('\n' + hashtags) if hashtags else ''
                        budget = 280 - len(fixed)
                        chunks = _sentence_chunks((base_text or '').strip(), max(0, budget))
                        first_body = chunks[0] if chunks else ''
                        remaining = ' '.join(chunks[1:]).strip()
                        first_text = (first_body + fixed).strip()
                        payload = {"text": first_text, "media": {"media_ids": media_ids[:4]}}
                        with httpx.Client(timeout=20) as h:
                            r = h.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
                            d = r.json()
                            if r.status_code >= 400:
                                raise RuntimeError(f"x_tweet_error status={r.status_code} body={d}")
                            tid = (d.get('data') or {}).get('id')
                            url = f"https://twitter.com/i/web/status/{tid}" if tid else None
                            while thread_enabled and remaining and tid:
                                segs = _sentence_chunks(remaining, 260)
                                if not segs:
                                    break
                                chunk = segs[0]
                                from app.services.x_client import XClient as _XC
                                xc = _XC(token)
                                tid = xc.reply(tid, chunk)
                                remaining = remaining[len(chunk):].lstrip()
                else:
                    # no media
                    import re
                    if not thread_enabled:
                        # Post full text in one tweet (XClient.tweet would silently cut at 280,
                        # so go through the raw API for long texts and fall back to a thread on 403).
                        full_text = (base_text or '').strip()
                        if hashtags:
                            full_text = (full_text + '\n' + hashtags).strip()
                        if len(full_text) <= 280:
                            tid2, url = client.tweet(full_text)
                        else:
                            try:
                                tid2 = _x_post_v2(full_text)
                            except RuntimeError as exc:
                                if 'status=403' in str(exc):
                                    tid2 = _x_thread_fallback()
                                    if job is not None:
                                        job.last_error = ((job.last_error or '') + ' x_280_limit_fallback_thread').strip()
                                        session.add(job)
                                        session.commit()
                                else:
                                    raise
                            url = f"https://twitter.com/i/web/status/{tid2}" if tid2 else None
                        if tid2:
                            post.platform_post_id = tid2
                    else:
                        def _sentence_chunks(text: str, limit: int) -> list[str]:
                            parts = re.findall(r"[^.!?]+[.!?]+|[^.!?]+$", text)
                            chunks = []
                            buf = ''
                            for s in parts:
                                s = s.strip()
                                if not s:
                                    continue
                                if not buf:
                                    buf = s
                                elif len(buf) + 1 + len(s) <= limit:
                                    buf += ' ' + s
                                else:
                                    chunks.append(buf)
                                    buf = s
                            if buf:
                                chunks.append(buf)
                            return chunks
                        fixed = ('\n' + hashtags) if hashtags else ''
                        budget = 280 - len(fixed)
                        chunks = _sentence_chunks((base_text or '').strip(), max(0, budget))
                        first_body = chunks[0] if chunks else ''
                        remaining = ' '.join(chunks[1:]).strip()
                        first_text = (first_body + fixed).strip()
                        tid2, url = client.tweet(first_text)
                        if tid2:
                            post.platform_post_id = tid2
                        tid = tid2
                        while remaining and tid:
                            segs = _sentence_chunks(remaining, 260)
                            if not segs:
                                break
                            chunk = segs[0]
                            tid = client.reply(tid, chunk)
                            remaining = remaining[len(chunk):].lstrip()
                post.platform_url = url
                if media_ids and tid:
                    post.platform_post_id = tid
            except Exception as exc:
                # Treat common Reddit websocket ingest errors as soft-success if a submission is created
                # We'll poll later to mark done
                try:
                    if 'Websocket error' in str(exc):
                        from app.jobs.tasks import refresh_metrics_for_post as _refresh
                        # best-effort: schedule an immediate refresh; status will flip if URL/ID present
                        _refresh.schedule(args=(post.id,), delay=5)
                        # keep running status but do not hard error
                        job.last_error = (job.last_error or '') + ' reddit_ws_softfail'
                        if job is not None:
                            session.add(job)
                        session.commit()
                        return 'ok'
                except Exception:
                    pass
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
        elif str(post.platform) == 'PlatformEnum.vk' or str(post.platform) == 'vk':
            from app.services.vk_client import VKClient
            cfg = load_settings()
            token = (cfg.get('vk_access_token') or '').strip()
            group_raw = (cfg.get('vk_group_id') or '').strip()
            if not token or not group_raw:
                post.status = PostStatusEnum.error
                if job is not None:
                    job.status = JobStatusEnum.error
                    missing_token = 'ok' if token else 'missing'
                    missing_group = 'ok' if group_raw else 'missing'
                    job.last_error = f'vk_not_configured: token={missing_token}, group_id={missing_group}'
                session.add(post)
                if job is not None:
                    session.add(job)
                session.commit()
                return 'error'
            # sanitize
            s = group_raw.lower().replace('club','').replace('public','').replace('event','')
            if s.startswith('-'):
                s = s[1:]
            s = ''.join(ch for ch in s if ch.isdigit())
            owner_id = f"-{s}"
            from app.models.models import Brief
            brief = session.get(Brief, post.brief_id)
            text = (brief.text_ru or brief.text_en or '') if brief else ''
            # Include hashtags on VK
            if brief and getattr(brief, 'tags_json', None):
                try:
                    text = (text + '\n' + brief.tags_json).strip()
                except Exception:
                    pass
            attachment = None
            # Use normal/horizontal media for VK (or override from job payload)
            media_rel_vk: Optional[str] = None
            try:
                import json as _json
                override = None
                gallery_list = []
                if job and job.payload_json:
                    try:
                        parsed = (_json.loads(job.payload_json) or {})
                        override = parsed.get('media_rel')
                        gallery_list = parsed.get('gallery') or []
                    except Exception:
                        override = None
                        gallery_list = []
                # Prefer normal/horizontal media
                media_rel_vk = override or getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
                if (not media_rel_vk) and gallery_list:
                    media_rel_vk = gallery_list[0]
            except Exception:
                media_rel_vk = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
            if media_rel_vk or gallery_list:
                try:
                    import os as _os, mimetypes as _m
                    from loguru import logger as _logger
                    vk = VKClient(token)
                    attachments: list[str] = []
                    candidates: list[str] = []
                    had_video = False
                    had_image = False
                    if gallery_list:
                        candidates = [str(x) for x in gallery_list if isinstance(x, str)]
                    if media_rel_vk and not candidates:
                        candidates = [media_rel_vk]
                    for rel in candidates[:10]:
                        path = rel.replace('\\', '/')
                        if not path.startswith('media/'):
                            path = f"media/{path}"
                        path = _os.path.abspath(path)
                        ctype = _m.guess_type(path)[0] or ''
                        _logger.info(f"vk.attach candidate path={path} ctype={ctype}")
                        if ctype.startswith('image/'):
                            had_image = True
                            try:
                                from app.services.settings import load_settings as _ls
                                _cfg = _ls()
                                user_tok = (_cfg.get('vk_user_access_token') or '').strip() or token
                                att = vk.upload_photo_to_wall(s, path, token_override=user_tok)
                                if att: attachments.append(att)
                            except Exception as _exc:
                                _logger.error(f"vk.photo upload failed: {_exc}", exc_info=True)
                                raise  # Прокинуть исключение дальше, чтобы сработала проверка had_image
                        elif ctype.startswith('video/'):
                            had_video = True
                            try:
                                # Use user access token for video.save (group tokens disallow it)
                                from app.services.settings import load_settings as _ls
                                _cfg = _ls()
                                user_tok = (_cfg.get('vk_user_access_token') or '').strip() or token
                                meta = vk.get_video_upload_server(s, name=_os.path.basename(path), token_override=user_tok)
                                _logger.info(f"vk.video.save meta keys={list((meta or {}).keys())} owner={meta.get('owner_id')} video_id={meta.get('video_id')}")
                                up_url = meta.get('upload_url')
                                up_resp = vk.upload_video(up_url, path) if up_url else {}
                                _logger.info(f"vk.video.upload resp keys={list((up_resp or {}).keys())}")
                                owner = meta.get('owner_id')
                                # video.save returns video_id; some SDKs use 'video_id' or 'id'
                                video_id = meta.get('video_id') or meta.get('id')
                                access_key = meta.get('access_key')
                                if not owner:
                                    # fall back to negative group id
                                    try:
                                        owner = f"-{int(s)}"
                                    except Exception:
                                        owner = f"-{s}"
                                if video_id:
                                    attach = f"video{owner}_{video_id}"
                                    if access_key:
                                        attach = f"{attach}_{access_key}"
                                    attachments.append(attach)
                                    _logger.info(f"vk.video attachment built: {attach}")
                                else:
                                    _logger.error(f"vk.video attach missing video_id owner={owner}")
                            except Exception as _exc:
                                _logger.error(f"vk.video upload failed: {_exc}")
                                if job is not None:
                                    job.last_error = (job.last_error or '') + f" vk_video_upload_failed: {_exc}"
                                    session.add(job)
                                    session.commit()
                    attachment = ','.join(attachments) if attachments else None
                    # If we intended to post media (image or video) but have no attachments, treat as error to avoid text-only fallback
                    if (attachment is None or not attachment.strip()) and (had_video or had_image):
                        raise RuntimeError('vk_media_upload_failed_no_attachment')
                except Exception as exc:
                    attachment = None
                    if job is not None:
                        job.last_error = f"vk_attach_failed: {exc}"
                        session.add(job)
                        session.commit()
                    # КРИТИЧНО: Если должно было быть медиа, но загрузка не удалась - прерываем весь процесс
                    if had_image or had_video:
                        post.status = PostStatusEnum.error
                        post.error_text = f"VK media upload failed: {exc}"
                        if job is not None:
                            job.status = JobStatusEnum.error
                            job.last_error = f"vk_media_upload_failed: {exc}"
                        session.add(post)
                        if job is not None:
                            session.add(job)
                        session.commit()
                        return 'error'
            # Продолжаем только если медиа не требовалось ИЛИ загрузилось успешно
            try:
                vk = VKClient(token)
                if attachment:
                    import httpx
                    params = {
                        "owner_id": owner_id,
                        "message": text,
                        "from_group": 1,
                        "attachments": attachment,
                        "v": "5.199",
                        "access_token": token,
                    }
                    with httpx.Client(timeout=httpx.Timeout(connect=10.0, read=60.0, write=60.0, pool=60.0)) as h:
                        r = h.post("https://api.vk.com/method/wall.post", data=params)
                        d = r.json()
                        if "error" in d:
                            raise RuntimeError(str(d["error"]))
                        post_id = (d.get("response") or {}).get("post_id") if isinstance(d, dict) else None
                        if post_id:
                            post.platform_post_id = f"{owner_id}_{post_id}"
                            post.platform_url = f"https://vk.com/wall{owner_id}_{post_id}"
                else:
                    resp = vk.wall_post(owner_id, text)
                    try:
                        post_id = (resp or {}).get("post_id") if isinstance(resp, dict) else None
                        if post_id:
                            post.platform_post_id = f"{owner_id}_{post_id}"
                            post.platform_url = f"https://vk.com/wall{owner_id}_{post_id}"
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
        elif str(post.platform) == 'PlatformEnum.yt' or str(post.platform) == 'yt':
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
            yt = YouTubeClient(
                access_token=(cfg.get('yt_access_token') or '').strip(),
                refresh_token=(cfg.get('yt_refresh_token') or '').strip(),
                client_id=(cfg.get('yt_client_id') or '').strip(),
                client_secret=(cfg.get('yt_client_secret') or '').strip(),
            )
            try:
                _logger.info(f"yt.upload start path={path} title_len={len(title or '')} desc_len={len(desc or '')}")
                vid, url = yt.upload_video(path, title=title, description=desc)
                post.platform_post_id = vid
                post.platform_url = url
                # Playlist handling
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
        elif str(post.platform) in ('PlatformEnum.fb_ru', 'fb_ru', 'PlatformEnum.fb_en', 'fb_en'):
            # Facebook Page posting (separate RU / EN pages)
            from app.services.facebook_client import FacebookClient
            from app.services.settings import load_settings
            from app.models.models import Brief
            cfg = load_settings()
            brief = session.get(Brief, post.brief_id)
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
                is_image = bool(path_abs and (_m.guess_type(path_abs)[0] or '').startswith('image/'))
                if path_abs and is_image:
                    _logger.info(f"fb.publish create_photo path={path_abs} msg_len={len(message or '')}")
                    resp = fb.create_photo(page_id, path_abs, caption=message)
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
        elif str(post.platform) in ('PlatformEnum.bluesky', 'bluesky'):
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
        elif str(post.platform) in ('PlatformEnum.reddit', 'reddit'):
            # Reddit EN only
            from app.services.reddit_client import RedditClient
            from app.services.settings import load_settings
            from app.models.models import Brief
            cfg = load_settings()
            cid = (cfg.get('reddit_client_id') or '').strip()
            csec = (cfg.get('reddit_client_secret') or '').strip()
            uname = (cfg.get('reddit_username') or '').strip()
            pwd = (cfg.get('reddit_password') or '').strip()
            sub = (cfg.get('reddit_subreddit_en') or '').strip()
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
            brief = session.get(Brief, post.brief_id)
            full_body = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or '')
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
        elif str(post.platform) in ('PlatformEnum.ig_reels', 'ig_reels'):
            # Instagram Reels via Graph API resumable upload; export fallback if unconfigured.
            from app.services.settings import load_settings
            from app.models.models import Brief
            cfg = load_settings()
            ig_user_id = (cfg.get('ig_user_id') or '').strip()
            ig_token = (cfg.get('ig_access_token') or '').strip()
            brief = session.get(Brief, post.brief_id)
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
            caption = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or getattr(brief, 'title', None) or '')
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
        else:
            # TikTok: try API first, fallback to export
            if str(post.platform) in ('PlatformEnum.tiktok', 'tiktok'):
                from app.services.settings import load_settings
                from app.services.tiktok_client import get_valid_access_token
                from app.models.models import Brief
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
            else:
                import time
                time.sleep(1)
        # Telegram (RU)
        if str(post.platform) in ('PlatformEnum.tg', 'tg'):
            try:
                from app.services.settings import load_settings
                from app.services.telegram_client import TelegramClient
                from app.models.models import Brief
                cfg = load_settings()
                bot = (cfg.get('tg_bot_token') or '').strip()
                chat_id = (cfg.get('tg_chat_id_ru') or '').strip()
                brief = session.get(Brief, post.brief_id)
                if not (bot and chat_id and brief):
                    raise RuntimeError('tg_not_configured')
                # No hashtags on Telegram — post the body text as-is, keeping its formatting.
                text = (brief.text_ru or brief.text_en or '')
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
        # mark done
        post.status = PostStatusEnum.done
        post.published_at = datetime.utcnow()
        if job is not None:
            job.status = JobStatusEnum.done
            session.add(job)
        session.add(post)
        session.commit()
        try:
            # kick off metrics refresh shortly after publish
            from app.jobs.tasks import refresh_metrics_for_post
            refresh_metrics_for_post.schedule(args=(post.id,), delay=2)
        except Exception:
            pass
    return "ok"


@huey.periodic_task(crontab(minute='*'))
def enqueue_due_posts() -> str:
    """Every minute, enqueue publish jobs for posts that reached their scheduled time
    but are still in queued status (useful for posts created before scheduling logic changed)."""
    from app.models.models import Post, PostStatusEnum, Job, JobKindEnum, JobStatusEnum
    from sqlmodel import select
    from datetime import datetime as _dt
    import json as _json
    with Session(engine) as session:
        # Use local naive time to match UI scheduling input
        now = _dt.now()
        rows = session.exec(
            select(Post).where(
                Post.status == PostStatusEnum.queued,
                Post.scheduled_at.is_not(None),
                Post.scheduled_at <= now,
            ).limit(100)
        ).all()
        scheduled = 0
        for post in rows:
            # Skip if there is already a queued/running job for this post
            existing = session.exec(
                select(Job).where(
                    Job.kind == JobKindEnum.publish,
                    Job.payload_json.contains(f'"post_id": {post.id}'),
                    Job.status.in_([JobStatusEnum.queued, JobStatusEnum.running])
                ).limit(1)
            ).first()
            if existing:
                continue
            payload = {"post_id": post.id}
            job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
            session.add(job)
            session.commit()
            session.refresh(job)
            publish_news_post.schedule(args=(job.id, post.id), delay=0)
            scheduled += 1
        return f"scheduled={scheduled}"


@huey.task()
def render_short(job_id: int, brief_id: int) -> str:
    """Render a batched short (9:16 vertical + poster frame), then create the
    per-platform Posts and schedule their publish jobs.

    Posts are created *after* the render (not at submit time) so the per-minute
    enqueue_due_posts cron can never publish a not-yet-rendered clip.
    """
    from app.models.models import Brief, Post, Job, PostStatusEnum, JobKindEnum, JobStatusEnum, PlatformEnum
    from app.services.video_editor import render_vertical_with_music, extract_poster_frame
    from loguru import logger
    import os, json
    from datetime import datetime as _dt
    with Session(engine) as session:
        job = session.get(Job, job_id)
        brief = session.get(Brief, brief_id)
        if brief is None:
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = 'brief_not_found'
                session.add(job)
                session.commit()
            return 'not_found'

        platforms: list = []
        scheduled_iso = None
        try:
            payload = json.loads(job.payload_json) if (job and job.payload_json) else {}
            platforms = payload.get('platforms') or []
            scheduled_iso = payload.get('scheduled_at')
        except Exception:
            platforms = []
        if job is not None:
            job.status = JobStatusEnum.running
            session.add(job)
            session.commit()

        # --- 1. Render the vertical clip (skip if already rendered) ---
        vertical_rel = getattr(brief, 'media_vertical_path', None)
        try:
            src = (brief.media_path or '').replace('\\', '/')
            if not src:
                raise RuntimeError('brief has no source media_path')
            if not src.startswith('media/'):
                src = f"media/{src}"
            src_abs = os.path.abspath(src)
            base = os.path.splitext(os.path.basename(src_abs))[0]
            out_rel = f"{base}_vertical.mp4"
            out_abs = os.path.abspath(os.path.join('media', out_rel))
            already = bool(vertical_rel) and os.path.exists(
                os.path.abspath(os.path.join('media', os.path.basename(vertical_rel)))
            )
            if not already:
                music_abs = None
                mp = getattr(brief, 'music_path', None)
                if mp and os.path.exists(mp):
                    music_abs = mp
                render_vertical_with_music(
                    src_abs, out_abs,
                    crop_x_pct=(brief.crop_x if brief.crop_x is not None else 50) / 100.0,
                    crop_y_pct=(brief.crop_y if brief.crop_y is not None else 50) / 100.0,
                    music_path=music_abs,
                    music_offset_s=float(brief.music_offset_sec or 0),
                    fade_in_s=float(brief.fade_in_ms or 0) / 1000.0,
                    fade_out_s=float(brief.fade_out_ms or 0) / 1000.0,
                    zoom=float(brief.crop_scale or 1.0),
                )
                vertical_rel = out_rel
                brief.media_vertical_path = vertical_rel
                session.add(brief)
                session.commit()
            # Poster frame for the board (best-effort)
            try:
                poster_abs = os.path.abspath(os.path.join('media', f"{base}_poster.jpg"))
                extract_poster_frame(out_abs, poster_abs, at_sec=1.0)
            except Exception:
                pass
        except Exception as exc:
            logger.error(f"render_short render failed brief={brief_id}: {exc}")
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = f'render_failed: {exc}'
                session.add(job)
                session.commit()
            return 'error'

        # --- 2. Create Posts + schedule publishes per platform ---
        when_dt = None
        if scheduled_iso:
            try:
                when_dt = _dt.fromisoformat(scheduled_iso)
                if getattr(when_dt, 'tzinfo', None) is not None:
                    when_dt = when_dt.replace(tzinfo=None)
            except Exception:
                when_dt = None
        made = 0
        for plat in platforms:
            try:
                pf = PlatformEnum(plat)
            except Exception:
                logger.warning(f"render_short: unknown platform {plat}")
                continue
            try:
                post = Post(user_id=1, brief_id=brief.id, platform=pf,
                            status=PostStatusEnum.queued, scheduled_at=when_dt)
                session.add(post)
                session.commit()
                session.refresh(post)
                pjob = Job(
                    kind=JobKindEnum.publish,
                    payload_json=json.dumps({"post_id": post.id, "media_rel": vertical_rel}, ensure_ascii=False),
                    status=JobStatusEnum.queued,
                )
                session.add(pjob)
                session.commit()
                session.refresh(pjob)
                delay_s = 0
                if when_dt:
                    delay_s = int(max(0, (when_dt - _dt.now()).total_seconds()))
                publish_news_post.schedule(args=(pjob.id, post.id), delay=delay_s)
                made += 1
            except Exception as exc:
                logger.error(f"render_short: failed to queue {plat}: {exc}")

        if job is not None:
            job.status = JobStatusEnum.done
            session.add(job)
            session.commit()
        return f"rendered brief={brief_id} posts={made}"


@huey.task()
def refresh_metrics_for_post(post_id: int) -> str:
    from app.models.models import Post, PostStatusEnum
    from app.services.settings import load_settings
    import json
    import httpx
    with Session(engine) as session:
        post = session.get(Post, post_id)
        if not post:
            return "not_found"
        platform = str(post.platform)
        metrics = {}
        try:
            if platform.endswith('x') and post.platform_post_id:
                cfg = load_settings()
                token = (cfg.get('x_access_token') or '').strip()
                if not token:
                    metrics = {"error": "x_not_connected"}
                else:
                    url = "https://api.twitter.com/2/tweets"
                    params = {"ids": post.platform_post_id, "tweet.fields": "public_metrics"}
                    headers = {"Authorization": f"Bearer {token}"}
                    with httpx.Client(timeout=15) as client:
                        r = client.get(url, params=params, headers=headers)
                        data = r.json()
                    pm = ((data.get("data") or [{}])[0]).get("public_metrics") or {}
                    metrics = {
                        "retweet": pm.get("retweet_count"),
                        "reply": pm.get("reply_count"),
                        "like": pm.get("like_count"),
                        "quote": pm.get("quote_count"),
                        "views": pm.get("impression_count"),
                        "bookmark": pm.get("bookmark_count"),
                    }
            elif platform.endswith('vk') and post.platform_post_id:
                cfg = load_settings()
                token = (cfg.get('vk_access_token') or '').strip()
                if not token:
                    metrics = {"error": "vk_not_connected"}
                else:
                    with httpx.Client(timeout=15) as client:
                        r = client.get("https://api.vk.com/method/wall.getById", params={
                            "posts": post.platform_post_id,
                            "v": "5.199",
                            "access_token": token,
                        })
                        data = r.json()
                    if isinstance(data, dict) and data.get("error"):
                        err = data.get("error") or {}
                        metrics = {"error": f"vk:{err.get('error_code')}:{err.get('error_msg')}"}
                    else:
                        # v5.199 returns response={"items":[...]}; older versions a bare list.
                        resp = data.get("response")
                        if isinstance(resp, dict):
                            items = resp.get("items") or []
                        elif isinstance(resp, list):
                            items = resp
                        else:
                            items = []
                        item = items[0] if items else {}
                        metrics = {
                            "likes": ((item.get("likes") or {}).get("count")),
                            "reposts": ((item.get("reposts") or {}).get("count")),
                            "comments": ((item.get("comments") or {}).get("count")),
                            "views": ((item.get("views") or {}).get("count")),
                        }
            elif platform.endswith('fb_ru') or platform.endswith('fb_en'):
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
            elif platform.endswith('reddit') and post.platform_post_id:
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
            elif platform.endswith('yt') and post.platform_post_id:
                # YouTube stats: requires access token
                cfg = load_settings()
                access = (cfg.get('yt_access_token') or '').strip()
                if not access:
                    metrics = {"error": "yt_not_connected"}
                else:
                    with httpx.Client(timeout=15) as client:
                        r = client.get("https://www.googleapis.com/youtube/v3/videos", params={
                            "part": "statistics",
                            "id": post.platform_post_id,
                        }, headers={"Authorization": f"Bearer {access}"})
                        data = r.json()
                    stats = ((data.get('items') or [{}])[0].get('statistics') or {})
                    likes = stats.get('likeCount')
                    comments = stats.get('commentCount')
                    views = stats.get('viewCount')
                    metrics = {"likes": int(likes) if likes is not None else None,
                               "comments": int(comments) if comments is not None else None,
                               "views": int(views) if views is not None else None}
            elif platform.endswith('bluesky') and post.platform_post_id:
                from app.services.bluesky_client import BlueskyClient
                metrics = BlueskyClient.get_post_metrics(post.platform_post_id) or {"error": "bluesky_fetch_failed"}
            elif platform.endswith('tg') and post.platform_post_id:
                # Bot API exposes no stats; for public channels we parse views from the t.me embed page
                cfg = load_settings()
                bot = (cfg.get('tg_bot_token') or '').strip()
                chat_id = (cfg.get('tg_chat_id_ru') or '').strip()
                views = None
                try:
                    if bot and chat_id:
                        from app.services.telegram_client import TelegramClient
                        views = TelegramClient(bot).get_channel_post_views(chat_id, int(post.platform_post_id))
                except Exception:
                    views = None
                metrics = {"views": views} if views is not None else {"status": "sent"}
            elif platform.endswith('discord'):
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
        except Exception as exc:
            metrics = {"error": str(exc)}
        # Backstop: if a platform post id or url exists but status is still running, mark it done
        try:
            if post.status == PostStatusEnum.running and (post.platform_post_id or post.platform_url):
                post.status = PostStatusEnum.done
        except Exception:
            pass
        post.metrics_json = json.dumps(metrics, ensure_ascii=False)
        session.add(post)
        session.commit()
        return "ok"


@huey.periodic_task(crontab(minute='*/30'))
def refresh_recent_metrics() -> str:
    """Every 30 minutes refresh metrics for posts published in the last 7 days,
    so Analytics stays fresh without manual clicking."""
    from app.models.models import Post, PostStatusEnum
    from sqlmodel import select
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(days=7)
    with Session(engine) as session:
        rows = session.exec(
            select(Post).where(
                Post.status == PostStatusEnum.done,
                Post.published_at.is_not(None),
                Post.published_at >= cutoff,
            ).order_by(Post.published_at.desc()).limit(60)
        ).all()
        ids = [p.id for p in rows]
    # Stagger requests to be gentle on platform APIs
    for i, pid in enumerate(ids):
        refresh_metrics_for_post.schedule(args=(pid,), delay=i * 2)
    return f"scheduled={len(ids)}"


@huey.task()
def reddit_reply_comment(submission_id: str, text: str, attempt: int = 1) -> str:
    """Post a follow-up comment to a Reddit submission, with simple retry backoff."""
    try:
        from app.services.settings import load_settings
        from app.services.reddit_client import RedditClient
        cfg = load_settings()
        cid = (cfg.get('reddit_client_id') or '').strip()
        csec = (cfg.get('reddit_client_secret') or '').strip()
        uname = (cfg.get('reddit_username') or '').strip()
        pwd = (cfg.get('reddit_password') or '').strip()
        if not (cid and csec and uname and pwd and submission_id):
            return "not_configured"
        rc = RedditClient(cid, csec, uname, pwd)
        rc.comment(submission_id, text[:10000])
        return "ok"
    except Exception as exc:
        # Retry a couple of times with backoff (Reddit can be slow to make posts commentable)
        if attempt < 5:
            reddit_reply_comment.schedule(args=(submission_id, text, attempt + 1), delay=min(300, 30 * attempt))
        return f"error: {exc}"
