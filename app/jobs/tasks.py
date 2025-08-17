from datetime import datetime
from typing import Optional
from .huey_app import huey
from sqlmodel import Session
from app.db.session import engine
from huey import crontab


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
            settings = load_settings()
            # Compose per-language messages and target webhooks
            from app.models.models import Brief
            brief = session.get(Brief, post.brief_id)
            text_ru = (brief.text_ru or '').strip() if brief else ''
            text_en = (brief.text_en or '').strip() if brief else ''
            media_path = getattr(brief, 'media_path', None) if brief else None
            if media_path:
                p = media_path.replace('\\', '/')
                if not p.startswith('media/'):
                    p = f"media/{p}"
                import os as _os
                media_path = _os.path.abspath(p)

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
            for wh_url, content in targets:
                try:
                    client = DiscordClient(wh_url)
                    _, murl = client.send_message(content, file_path=media_path)
                    last_url = murl or last_url
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
            base_text = (brief.text_en or brief.text_ru or '') if brief else ''
            hashtags = ''
            if brief and getattr(brief, 'tags_json', None):
                try:
                    hashtags = (brief.tags_json or '').strip()
                except Exception:
                    hashtags = ''
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
                    # Build thread: first tweet with text+hashtags limited to 280; if overflow, continue in replies
                    text1 = (base_text + (('\n'+hashtags) if hashtags else ''))
                    payload = {"text": text1[:280], "media": {"media_ids": [media_id]}}
                    with httpx.Client(timeout=20) as h:
                        r = h.post("https://api.twitter.com/2/tweets", json=payload, headers=headers)
                        d = r.json()
                        if r.status_code >= 400:
                            raise RuntimeError(f"x_tweet_error status={r.status_code} body={d}")
                        tid = (d.get('data') or {}).get('id')
                        url = f"https://twitter.com/i/web/status/{tid}" if tid else None
                        # If overflow beyond 280, post remaining text as replies (split on word boundaries)
                        remaining = text1[280:]
                        import re
                        while remaining and tid:
                            chunk = remaining[:260]
                            # try to break on whitespace
                            m = re.search(r"^(.{1,260})(\s|$)", remaining)
                            if m:
                                chunk = m.group(1)
                            from app.services.x_client import XClient as _XC
                            xc = _XC(token)
                            tid = xc.reply(tid, chunk)
                            remaining = remaining[len(chunk):].lstrip()
                else:
                    # no media; thread similarly
                    text1 = (base_text + (('\n'+hashtags) if hashtags else ''))
                    tid2, url = client.tweet(text1)
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
            # Include hashtags on VK
            if brief and getattr(brief, 'tags_json', None):
                try:
                    text = (text + '\n' + brief.tags_json).strip()
                except Exception:
                    pass
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
            # Split first paragraph as header and remainder for comment
            first_par = full_body.strip().split('\n\n', 1)[0] if full_body else ''
            rest_text = ''
            if '\n\n' in full_body:
                rest_text = full_body.split('\n\n', 1)[1].strip()
            elif len(full_body) > 500:
                first_par, rest_text = full_body[:500], full_body[500:]
            # Title from first paragraph; fallback to explicit title
            title = ((getattr(brief, 'title', None) or first_par or getattr(brief, 'text_en', None) or 'Post from IndiePulse'))[:290]
            body = ''  # main post body left empty; we will add the remainder as a comment
            if brief and getattr(brief, 'tags_json', None):
                try:
                    body = (body + '\n' + brief.tags_json).strip()
                except Exception:
                    pass
            try:
                rc = RedditClient(cid, csec, uname, pwd)
                # choose media if present
                media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
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
                        sid, url = rc.submit_video(sub, title=title, video_path=path)
                if not sid:
                    sid, url = rc.submit_text(sub, title=title, text=body)
                post.platform_post_id = sid
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
            # Build a message if needed for FB branch; Reddit handled above
            message = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or '')
            # include hashtags for FB
            if brief and getattr(brief, 'tags_json', None):
                try:
                    message = (message + '\n' + brief.tags_json).strip()
                except Exception:
                    pass
            media_rel = getattr(brief, 'media_normal_path', None) or getattr(brief, 'media_path', None)
            path_abs = None
            if media_rel:
                p = media_rel.replace('\\','/')
                if not p.startswith('media/'):
                    p = f"media/{p}"
                import os as _os
                path_abs = _os.path.abspath(p)
            try:
                from app.services.facebook_client import FacebookClient as _FB
                fb = _FB(page_token)
                if path_abs:
                    resp = fb.create_photo(page_id, path_abs, caption=message)
                else:
                    resp = fb.create_post(page_id, message)
                post_id = str(resp.get('post_id') or resp.get('id') or '')
                post.platform_post_id = post_id
                try:
                    post.platform_url = fb.get_permalink(post_id)
                except Exception:
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
            path = media_rel.replace('\\', '/')
            if not path.startswith('media/'):
                path = f"media/{path}"
            import os as _os
            path = _os.path.abspath(path)
            # Title preference: use explicit yt_title from job/brief if present, else EN then RU
            title = None
            try:
                import json as _json
                override = None
                if job and job.payload_json:
                    try:
                        override = (_json.loads(job.payload_json) or {}).get('yt_title')
                    except Exception:
                        override = None
                if override:
                    title = override
            except Exception:
                pass
            if not title:
                title = (getattr(brief, 'title', None) or getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or 'IndiePulse Short')[:90]
            # For YouTube, include hashtags in description as well
            desc = 'Uploaded via IndiePulse'
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
                vid, url = yt.upload_short(path, title=title, description=desc)
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
            # TikTok export: copy vertical media into export dir for manual upload
            if str(post.platform) in ('PlatformEnum.tiktok', 'tiktok'):
                from app.services.settings import load_settings
                from app.models.models import Brief
                cfg = load_settings()
                export_dir = (cfg.get('tiktok_export_dir') or 'render').strip() or 'render'
                brief = session.get(__import__('app.models.models', fromlist=['Brief']).Brief, post.brief_id)
                media_rel = getattr(brief, 'media_vertical_path', None) or getattr(brief, 'media_path', None)
                if media_rel:
                    import os as _os, shutil as _sh
                    p = media_rel.replace('\\','/')
                    if not p.startswith('media/'):
                        p = f"media/{p}"
                    src = _os.path.abspath(p)
                    _os.makedirs(export_dir, exist_ok=True)
                    dst = _os.path.join(export_dir, _os.path.basename(src))
                    try:
                        _sh.copy2(src, dst)
                        post.platform_url = None
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
