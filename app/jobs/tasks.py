from datetime import datetime
from typing import Optional
from .huey_app import huey
from sqlmodel import Session
from app.db.session import engine


@huey.task()
def publish_news_post(job_id: int, post_id: int) -> str:
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
        # mark running
        post.status = PostStatusEnum.running
        if job is not None:
            job.status = JobStatusEnum.running
            session.add(job)
        session.add(post)
        session.commit()
        # Simulate publish work; if discord, try sending
        if str(post.platform) == 'PlatformEnum.discord' or str(post.platform) == 'discord':
            url = None
            url = load_settings().get('discord_webhook_url')
            if url:
                client = DiscordClient(url)
                try:
                    # Compose message and include media if any
                    from app.models.models import Brief
                    brief = session.get(Brief, post.brief_id)
                    text = (brief.text_en or brief.text_ru or '') if brief else '[IndiePulse] New post'
                    media_path = getattr(brief, 'media_path', None) if brief else None
                    if media_path:
                        p = media_path.replace('\\', '/')
                        if not p.startswith('media/'):
                            p = f"media/{p}"
                        # Use absolute path to avoid CWD issues
                        import os as _os
                        media_path = _os.path.abspath(p)
                    _, murl = client.send_message(text, file_path=media_path)
                    post.platform_url = murl
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
        elif str(post.platform) == 'PlatformEnum.x' or str(post.platform) == 'x':
            from app.services.x_client import XClient
            from app.services.x_media import XMediaUploader
            cfg = load_settings()
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
            text = (brief.text_en or brief.text_ru or '') if brief else ''
            media_id: Optional[str] = None
            # prefer normal video/image if provided via job payload override or draft hidden field
            media_rel: Optional[str] = None
            try:
                import json as _json
                override = None
                if job and job.payload_json:
                    try:
                        override = (_json.loads(job.payload_json) or {}).get('media_rel')
                    except Exception:
                        override = None
                media_rel = override or getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
            except Exception:
                media_rel = getattr(brief, 'media_path', None)
            if media_rel:
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
                        media_id = uploader.upload(path)
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
            try:
                if media_id:
                    # Use v2 create tweet with media
                    import httpx
                    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
                    payload = {"text": text[:280], "media": {"media_ids": [media_id]}}
                    with httpx.Client(timeout=20) as h:
                        r = h.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
                        d = r.json()
                        if r.status_code >= 400:
                            raise RuntimeError(f"x_tweet_error status={r.status_code} body={d}")
                        tid = (d.get('data') or {}).get('id')
                        url = f"https://twitter.com/i/web/status/{tid}" if tid else None
                else:
                    tid2, url = client.tweet(text)
                    if tid2:
                        post.platform_post_id = tid2
                post.platform_url = url
                if media_id and tid:
                    post.platform_post_id = tid
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
            attachment = None
            # prefer normal media for VK (or override from job payload)
            media_rel_vk: Optional[str] = None
            try:
                import json as _json
                override = None
                if job and job.payload_json:
                    try:
                        override = (_json.loads(job.payload_json) or {}).get('media_rel')
                    except Exception:
                        override = None
                media_rel_vk = override or getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
            except Exception:
                media_rel_vk = getattr(brief, 'media_path', None)
            if media_rel_vk:
                try:
                    import os as _os, mimetypes as _m
                    vk = VKClient(token)
                    path = media_rel_vk
                    if path:
                        path = path.replace('\\', '/')
                        if not path.startswith('media/'):
                            path = f"media/{path}"
                        path = _os.path.abspath(path)
                    ctype = _m.guess_type(path)[0] or ''
                    if ctype.startswith('image/'):
                        attachment = vk.upload_photo_to_wall(s, path)
                    elif ctype.startswith('video/'):
                        # Fallback: no wall video upload yet; include no attachment and rely on link/text
                        if job is not None:
                            job.last_error = (job.last_error or '') + ' vk_video_upload_not_implemented'
                            session.add(job)
                            session.commit()
                except Exception as exc:
                    attachment = None
                    if job is not None:
                        job.last_error = f"vk_photo_upload_failed: {exc}"
                        session.add(job)
                        session.commit()
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
                    with httpx.Client(timeout=20) as h:
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
            # YouTube Shorts upload using vertical media if present
            from app.services.youtube_client import YouTubeClient
            from app.services.settings import load_settings
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
                media_rel = override or getattr(brief, 'media_vertical_path', None) or getattr(brief, 'media_path', None)
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
            title = (brief.text_en or brief.text_ru or 'IndiePulse Short')[:90] if brief else 'IndiePulse Short'
            yt = YouTubeClient(
                access_token=(cfg.get('yt_access_token') or '').strip(),
                refresh_token=(cfg.get('yt_refresh_token') or '').strip(),
                client_id=(cfg.get('yt_client_id') or '').strip(),
                client_secret=(cfg.get('yt_client_secret') or '').strip(),
            )
            try:
                vid, url = yt.upload_short(path, title=title, description='Uploaded via IndiePulse')
                post.platform_post_id = vid
                post.platform_url = url
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
        else:
            import time
            time.sleep(1)
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


@huey.task()
def refresh_metrics_for_post(post_id: int) -> str:
    from app.models.models import Post
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
                    item = ((data.get("response") or [{}])[0])
                    likes = ((item.get("likes") or {}).get("count"))
                    reposts = ((item.get("reposts") or {}).get("count"))
                    comments = ((item.get("comments") or {}).get("count"))
                    views = ((item.get("views") or {}).get("count"))
                    metrics = {"likes": likes, "reposts": reposts, "comments": comments, "views": views}
            elif platform.endswith('discord'):
                metrics = {"status": "sent"}
        except Exception as exc:
            metrics = {"error": str(exc)}
        post.metrics_json = json.dumps(metrics, ensure_ascii=False)
        session.add(post)
        session.commit()
        return "ok"
