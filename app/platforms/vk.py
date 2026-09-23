"""VK wall posts (also used for vk_video)."""
from __future__ import annotations

from typing import Optional


def publish(session, post, job) -> Optional[str]:
    """Publish one post. Return None on success (the caller marks it done),
    or a result string ('error', 'ok', ...) after setting the final state itself."""
    from app.models.models import PostStatusEnum, JobStatusEnum
    from app.services.settings import load_settings
    import os
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


def fetch_metrics(post) -> dict:
    """Engagement counters for a published post; may raise (caller records the error)."""
    from app.services.settings import load_settings
    import httpx
    metrics: dict = {}
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
    return metrics
