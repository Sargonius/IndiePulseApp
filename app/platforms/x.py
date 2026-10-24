"""X / Twitter (OAuth2 for tweets, OAuth1 for media upload)."""
from __future__ import annotations

from typing import Optional


def compose(brief, post=None) -> tuple[str, str]:
    """(body, hashtags) of the tweet for a brief: in the language of the post's destination,
    or EN first for a post without one."""
    if not brief:
        return '', ''
    from app.platforms.common import post_text
    return post_text(brief, post, ('en', 'ru')), (getattr(brief, 'tags_json', None) or '').strip()


def thread_parts(body: str, tags: str) -> list[str]:
    """280-safe thread: the first tweet keeps the hashtags, the rest go as replies."""
    from app.services.bluesky_client import _sentence_chunks
    fixed = ('\n' + tags) if tags else ''
    chunks = _sentence_chunks((body or '').strip(), max(40, 280 - len(fixed)))
    first = ((chunks[0] if chunks else '') + fixed).strip() or tags
    return [first] + chunks[1:]


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
    from app.platforms.common import brief_for_publish
    brief = brief_for_publish(session, post)
    base_text, hashtags = compose(brief, post)
    media_ids: list[str] = []
    # Media from the job payload (gallery, else media_rel), else the brief's own file
    files: list[str] = []
    try:
        import json as _json
        parsed = (_json.loads(job.payload_json) or {}) if (job and job.payload_json) else {}
        gallery = parsed.get('gallery')
        if isinstance(gallery, list):
            files = [str(x) for x in gallery if isinstance(x, str)]
        if not files and parsed.get('media_rel'):
            files = [str(parsed['media_rel'])]
    except Exception:
        files = []
    if not files:
        brief_media = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
        files = [brief_media] if brief_media else []
    abs_files = []
    for rel in files:
        path = rel.replace('\\', '/')
        if not path.startswith('media/'):
            path = f"media/{path}"
        abs_files.append(os.path.abspath(path))
    # X takes one video or up to 4 images. A post with media never goes out as a
    # bare text tweet: any upload problem fails the post (auto-retried once).
    import mimetypes as _m
    video = next((p for p in abs_files if (_m.guess_type(p)[0] or '').startswith('video/')), None)
    uploads = [video] if video else abs_files[:4]
    if uploads:
        api_key = (cfg.get('x_api_key') or '').strip()
        api_secret = (cfg.get('x_api_secret') or '').strip()
        oauth_token = (cfg.get('x_oauth_token') or '').strip()
        oauth_secret = (cfg.get('x_oauth_token_secret') or '').strip()
        try:
            if not (api_key and api_secret and oauth_token and oauth_secret):
                raise RuntimeError("OAuth1 keys for media upload are missing (Accounts → X)")
            if video:
                from app.services.video_editor import ensure_video_size_limit
                uploads = [ensure_video_size_limit(video, 'x', cfg)]
            uploader = XMediaUploader(oauth_token, oauth_secret, api_key, api_secret)
            for path in uploads:
                media_ids.append(uploader.upload(path))
        except Exception as exc:
            post.status = PostStatusEnum.error
            post.error_text = f"x_media_upload_failed: {exc} — not posting the text without its media"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            return 'error'
    import httpx as _httpx
    from app.services import x_billing
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
        if x_billing.remember(r.status_code, d):
            raise RuntimeError(x_billing.ERROR_TEXT)
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
        """First tweet keeps hashtags (+media), the rest go as replies."""
        parts = thread_parts(base_text, hashtags)
        first_id = _x_post_v2(parts[0], media=media)
        parent = first_id
        for seg in parts[1:]:
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
        from app.services import x_billing
        if x_billing.remember(r.status_code, data):
            raise RuntimeError(x_billing.ERROR_TEXT)
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
