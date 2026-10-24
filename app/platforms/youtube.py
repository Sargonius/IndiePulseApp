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
    from app.platforms.common import brief_for_publish
    brief = brief_for_publish(session, post)
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
    # the text in the destination's language (EN first for a post without one)
    from app.platforms.common import post_text
    body = post_text(brief, post, ('en', 'ru'), cfg)
    if not title:
        title = (getattr(brief, 'title', None) or body or 'IndiePulse Short')[:90]
    # Description with hashtags
    desc = (body or 'Uploaded via IndiePulse')
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
            err_text = ("yt_reauth_needed: refresh token expired/revoked — "
                        "click Connect YouTube in Settings. " + err_text)
        post.error_text = err_text
        if job is not None:
            job.status = JobStatusEnum.error
            job.last_error = err_text
        session.add(post)
        if job is not None:
            session.add(job)
        session.commit()
        return 'error'


VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
# videos.list takes up to 50 ids and costs 1 quota unit per call however many ids it
# carries, so stats are always fetched in batches (uploads need the rest of the quota).
MAX_IDS_PER_CALL = 50
VIDEO_NOT_FOUND = ("yt_video_not_found: not in the videos.list response — deleted/removed, "
                   "or private and not owned by the connected account")


def _count(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _api_error(resp) -> str:
    """'http_403 quotaExceeded: The request cannot be completed because ...'"""
    try:
        body = resp.json()
    except ValueError:
        body = None
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict):
        return f"http_{resp.status_code}: {resp.text[:200]}"
    reason = (err.get("errors") or [{}])[0].get("reason") or err.get("status") or ""
    return f"http_{resp.status_code} {reason}: {(err.get('message') or '')[:200]}"


def _auth_error(exc) -> str:
    return f"yt_reauth_needed: {exc}" if getattr(exc, "reauth", False) else f"yt_token_error: {exc}"


def _metrics_from_item(item: dict) -> dict:
    stats = item.get("statistics") or {}
    metrics = {"likes": _count(stats.get("likeCount")),
               "comments": _count(stats.get("commentCount")),
               "views": _count(stats.get("viewCount"))}
    if all(v is None for v in metrics.values()):
        st = item.get("status") or {}
        detail = ", ".join(f"{k}={st[k]}" for k in
                           ("privacyStatus", "uploadStatus", "rejectionReason", "failureReason") if st.get(k))
        return {"error": f"yt_no_statistics ({detail or 'no status'})"}
    return metrics


def fetch_metrics_batch(video_ids) -> dict:
    """{video_id: metrics} for any number of videos, one videos.list call per 50 ids.

    Never hands back all-None counters: a video missing from the response, a failed
    call or a dead token come back as {"error": ...} for the affected ids, so the
    caller records the failure instead of blank numbers.
    """
    import httpx
    from loguru import logger
    from app.services.tokens import yt_access_token

    ids = list(dict.fromkeys(v.strip() for v in video_ids if v and v.strip()))
    out: dict = {}
    if not ids:
        return out
    try:
        access = yt_access_token()
    except Exception as exc:
        return {vid: {"error": _auth_error(exc)} for vid in ids}

    def _get(client, chunk):
        return client.get(VIDEOS_URL, params={"part": "statistics,status", "id": ",".join(chunk)},
                          headers={"Authorization": f"Bearer {access}"})

    with httpx.Client(timeout=20) as client:
        for start in range(0, len(ids), MAX_IDS_PER_CALL):
            chunk = ids[start:start + MAX_IDS_PER_CALL]
            try:
                r = _get(client, chunk)
                if r.status_code == 401:
                    # the stored expiry can't see a revoked token: refresh once and retry
                    access = yt_access_token(force=True)
                    r = _get(client, chunk)
            except httpx.HTTPError as exc:
                out.update({vid: {"error": f"yt_metrics_network: {exc}"} for vid in chunk})
                continue
            except Exception as exc:
                # the forced refresh failed: every remaining call would fail the same way
                out.update({vid: {"error": _auth_error(exc)} for vid in ids[start:]})
                break
            try:
                data = r.json() if r.status_code < 400 else None
            except ValueError:
                data = None
            if not isinstance(data, dict):
                msg = f"yt_metrics_{_api_error(r)}"
                logger.warning(f"yt.metrics videos.list failed for {len(chunk)} ids: {msg}")
                out.update({vid: {"error": msg} for vid in chunk})
                continue
            items = {it.get("id"): it for it in (data.get("items") or [])}
            missing = [vid for vid in chunk if vid not in items]
            if missing:
                logger.warning(f"yt.metrics videos.list returned {len(items)}/{len(chunk)} videos, "
                               f"missing={missing} response={str(data)[:500]}")
            for vid in chunk:
                out[vid] = _metrics_from_item(items[vid]) if vid in items else {"error": VIDEO_NOT_FOUND}
    return out


def fetch_metrics(post) -> dict:
    """Engagement counters for one published video (a batch of one)."""
    vid = (post.platform_post_id or "").strip()
    return fetch_metrics_batch([vid]).get(vid) or {}
