"""X / Twitter (OAuth2 for tweets, OAuth1 for media upload)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    from app.services.settings import load_settings
    import os
    from app.services.x_media import XMediaUploader
    cfg = load_settings()
    # Ensure/refresh token
    try:
        from app.services.tokens import x_ensure_token
        token = x_ensure_token()
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
        tid_ = (d.get('data') or {}).get('id')
        if tid_ and not reply_to and not post.platform_post_id:
            # Persist the main tweet id immediately: if a later thread reply fails,
            # the retry guard sees the post already landed and won't double-post.
            post.platform_post_id = tid_
            post.platform_url = f"https://twitter.com/i/web/status/{tid_}"
            session.add(post)
            session.commit()
        return tid_

    def _x_thread(media: Optional[list] = None) -> Optional[str]:
        """280-safe thread: first tweet keeps hashtags (+media), the rest go as replies."""
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

    thread_enabled = False
    try:
        import json as _json
        if job and job.payload_json:
            thread_enabled = (_json.loads(job.payload_json) or {}).get('x_thread') is True
    except Exception:
        thread_enabled = False
    try:
        if thread_enabled:
            _x_thread(media=media_ids or None)
        else:
            # Full text in one tweet (>280 works with Premium). Without Premium X
            # answers 403 for long texts — fall back to a thread automatically.
            full_text = (base_text or '').strip()
            if hashtags:
                full_text = (full_text + '\n' + hashtags).strip()
            try:
                _x_post_v2(full_text, media=media_ids or None)
            except RuntimeError as exc:
                if len(full_text) > 280 and 'status=403' in str(exc):
                    _x_thread(media=media_ids or None)
                    if job is not None:
                        job.last_error = ((job.last_error or '') + ' x_280_limit_fallback_thread').strip()
                        session.add(job)
                        session.commit()
                else:
                    raise
    except Exception as exc:
        if post.platform_post_id:
            # Main tweet is live; only a thread reply failed. Keep it 'done'.
            if job is not None:
                job.last_error = ((job.last_error or '') + f' x_thread_partial: {exc}').strip()
                session.add(job)
                session.commit()
        else:
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
    import httpx
    metrics: dict = {}
    from app.services.tokens import x_ensure_token
    try:
        token = x_ensure_token()
    except Exception as exc:
        token = ''
        metrics = {"error": f"x_token: {exc}"}
    if token:
        url = "https://api.twitter.com/2/tweets"
        params = {"ids": post.platform_post_id, "tweet.fields": "public_metrics"}
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=15) as client:
            r = client.get(url, params=params, headers=headers)
            data = r.json()
        if r.status_code >= 400:
            raise RuntimeError(f"x_metrics status={r.status_code} body={str(data)[:200]}")
        pm = ((data.get("data") or [{}])[0]).get("public_metrics") or {}
        metrics = {
            "retweet": pm.get("retweet_count"),
            "reply": pm.get("reply_count"),
            "like": pm.get("like_count"),
            "quote": pm.get("quote_count"),
            "views": pm.get("impression_count"),
            "bookmark": pm.get("bookmark_count"),
        }
    return metrics
