from datetime import datetime, timedelta
from .huey_app import huey
from sqlmodel import Session
from app.db.session import engine
from huey import crontab
from app.core import modules


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
        from app.services.x_billing import DEPLETED
        if DEPLETED in (post.error_text or ''):
            return  # an empty X balance isn't transient; Retry after the top-up
        from app.services.video_editor import TOO_LARGE
        if TOO_LARGE in (post.error_text or ''):
            return  # compression already failed; a rerun would fail the same way
        if any(m in (post.error_text or '') for m in (MISSED, MODULE_OFF, DESTINATION_OFF)):
            return  # not a failure: waits for the user's Retry
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
        # Only a queued post may start. 'running' means another run is in flight or
        # crashed mid-publish — re-running could double-post (watchdog handles those).
        if post.status != PostStatusEnum.queued:
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

        if _missed(post):
            _mark_missed(session, post, job)
            from loguru import logger
            logger.warning(f"publish post={post_id} skipped: {post.error_text}")
            return 'missed'

        if not modules.platform_on(post.platform):
            # turned off on 🧩 Modules after the post was queued
            post.status = PostStatusEnum.error
            name = getattr(post.platform, 'value', post.platform)
            post.error_text = f"{MODULE_OFF}: {name} is turned off in 🧩 Modules — turn it on, then Retry"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            return 'module_off'

        if getattr(post, 'destination_id', None):
            # its destination was removed or turned off in Settings → Languages & destinations after the post
            # was queued: never fall back to some other channel
            from app.services import destinations
            why = destinations.unavailable(post.destination_id)
            if why is not None:
                post.status = PostStatusEnum.error
                post.error_text = (f"{DESTINATION_OFF}: {post.destination_id} was "
                                   f"{'removed' if why == 'removed' else 'turned off'} in Settings → Languages & "
                                   f"destinations — {'add it back' if why == 'removed' else 'turn it on'}, then Retry")
                if job is not None:
                    job.status = JobStatusEnum.error
                    job.last_error = post.error_text
                    session.add(job)
                session.add(post)
                session.commit()
                return 'destination_off'
            # a destination that takes only its own language, and the post has no text in it
            from app.platforms.common import brief_for_publish
            dest = destinations.get(post.destination_id)
            if destinations.lacks_own_text(dest, brief_for_publish(session, post)):
                post.status = PostStatusEnum.canceled
                post.error_text = (f"{NO_TEXT}: no {dest['lang'].upper()} text for {destinations.title(dest)}, "
                                   f"which takes only its own language — nothing sent there")
                if job is not None:
                    job.status = JobStatusEnum.done
                    job.last_error = post.error_text
                    session.add(job)
                session.add(post)
                session.commit()
                return 'no_text'

        # Never send the same publication twice (a re-submitted form, a second Shorts batch...)
        import json as _json
        from app.services import duplicates
        try:
            allow_dup = bool((_json.loads(job.payload_json or '{}') or {}).get(duplicates.ALLOW)) if job else False
        except Exception:
            allow_dup = False
        twin = None if allow_dup else duplicates.published_twin(session, post)
        if twin is not None:
            post.status = PostStatusEnum.canceled
            post.error_text = duplicates.reason(twin)
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            from loguru import logger
            logger.warning(f"publish post={post_id} skipped: {post.error_text}")
            return 'duplicate'

        # mark running
        post.status = PostStatusEnum.running
        if job is not None:
            job.status = JobStatusEnum.running
            session.add(job)
        session.add(post)
        session.commit()
        # Simulate publish work; if discord, try sending
        from app.platforms import PUBLISHERS, platform_key
        publisher = PUBLISHERS.get(platform_key(post.platform))
        if publisher is None:
            # Unknown platform: never fall through to "mark done" without publishing.
            post.status = PostStatusEnum.error
            post.error_text = f"unsupported_platform: {post.platform}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            return 'error'
        try:
            # follower counts right before the post, for its "+N followers" report
            from app.services.marketing_collect import snapshot_before_publish
            snapshot_before_publish()
        except Exception as exc:
            from loguru import logger
            logger.warning(f"pre-publish follower snapshot failed: {exc}")
        try:
            result = publisher(session, post, job)
        except Exception as exc:
            # A publisher that raises must not leave the post stuck in 'running'.
            from loguru import logger
            logger.exception(f"publish {post.platform} post={post_id} crashed")
            session.rollback()
            post.status = PostStatusEnum.error
            post.error_text = f"{type(exc).__name__}: {exc}"
            if job is not None:
                job.status = JobStatusEnum.error
                job.last_error = post.error_text
                session.add(job)
            session.add(post)
            session.commit()
            return 'error'
        if result is not None:
            return result
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


def find_publish_jobs(session, post_id: int) -> list:
    """Publish jobs for exactly this post, newest first.

    payload_json is plain text, so a bare contains('"post_id": 5') also matched
    posts 50, 51, ... — pre-filter with LIKE, then confirm by parsing the JSON.
    """
    import json as _json
    from sqlmodel import select
    from app.models.models import Job, JobKindEnum
    rows = session.exec(
        select(Job).where(
            Job.kind == JobKindEnum.publish,
            Job.payload_json.contains(f'"post_id": {int(post_id)}'),
        ).order_by(Job.id.desc())
    ).all()
    out = []
    for j in rows:
        try:
            if int((_json.loads(j.payload_json or '{}') or {}).get('post_id') or 0) == int(post_id):
                out.append(j)
        except Exception:
            continue
    return out


def publish_payload_for(session, post_id: int) -> dict:
    """Payload of the post's latest publish job (media_rel, gallery, yt_* options...),
    so re-enqueued jobs don't silently drop the media the user picked."""
    import json as _json
    for j in find_publish_jobs(session, post_id):
        try:
            payload = _json.loads(j.payload_json or '{}') or {}
        except Exception:
            continue
        payload.pop('publish_attempt', None)
        payload['post_id'] = int(post_id)
        return payload
    return {"post_id": int(post_id)}


STUCK_RUNNING_MIN = 90   # a publish that "runs" this long has died with the worker
LOST_QUEUED_MIN = 15     # a due post whose queued job hasn't started in this long was lost
MISSED_AFTER_MIN = 120   # a scheduled post this late isn't published by itself any more (see _missed)
MISSED = "missed_schedule"
MODULE_OFF = "module_off"
DESTINATION_OFF = "destination_off"
NO_TEXT = "no_text_in_language"


def _missed(post, now=None) -> bool:
    """Due long ago and nobody has said "publish now" since (Publish Now and Retry drop scheduled_at): IndiePulse
    wasn't running at that time — the drive it lives on was unplugged, the computer off. On the next start Huey
    would run every overdue task at once and days of posts would go out in one burst; such a post waits for
    the user instead."""
    from datetime import datetime as _dt, timedelta as _td
    target = getattr(post, 'scheduled_at', None)
    if target is None:
        return False
    if getattr(target, 'tzinfo', None) is not None:
        target = target.replace(tzinfo=None)  # scheduled_at is local naive time, as the UI enters it
    return target <= (now or _dt.now()) - _td(minutes=MISSED_AFTER_MIN)


def _mark_missed(session, post, job=None) -> None:
    from app.models.models import PostStatusEnum, JobStatusEnum
    post.status = PostStatusEnum.error
    post.error_text = (f"{MISSED}: IndiePulse wasn't running at {post.scheduled_at:%d.%m %H:%M} — "
                       f"Retry publishes it now, or set a new time")
    session.add(post)
    for j in [job] if job is not None else find_publish_jobs(session, post.id):
        if j.status in (JobStatusEnum.queued, JobStatusEnum.running):
            j.status = JobStatusEnum.error
            j.last_error = ((j.last_error or '') + f' {MISSED}').strip()
            session.add(j)
    session.commit()


@huey.periodic_task(crontab(minute='*'))
def enqueue_due_posts() -> str:
    """Every minute:
    - enqueue publish jobs for posts that reached their scheduled time but are still queued;
    - re-enqueue due posts whose queued job was lost (e.g. queue.db wiped / worker died);
    - flag posts stuck in 'running' (worker crashed mid-publish) instead of leaving them forever.
    """
    from app.models.models import Post, PostStatusEnum, Job, JobKindEnum, JobStatusEnum
    from sqlmodel import select
    from datetime import datetime as _dt, timedelta as _td
    import json as _json
    with Session(engine) as session:
        utc_now = _dt.utcnow()
        # Stuck 'running' posts. Never auto-republish: the platform may already have it.
        stuck = session.exec(
            select(Post).where(
                Post.status == PostStatusEnum.running,
                Post.updated_at < utc_now - _td(minutes=STUCK_RUNNING_MIN),
            ).limit(100)
        ).all()
        for post in stuck:
            if post.platform_post_id or post.platform_url:
                post.status = PostStatusEnum.done
            else:
                post.status = PostStatusEnum.error
                post.error_text = ((post.error_text or '') + ' stuck_running: the worker stopped mid-publish — '
                                   'check the platform before retrying').strip()
            session.add(post)
            for j in find_publish_jobs(session, post.id):
                if j.status == JobStatusEnum.running:
                    j.status = JobStatusEnum.error
                    j.last_error = ((j.last_error or '') + ' stuck_running').strip()
                    session.add(j)
        if stuck:
            session.commit()

        # Use local naive time to match UI scheduling input
        now = _dt.now()
        rows = session.exec(
            select(Post).where(
                Post.status == PostStatusEnum.queued,
                Post.scheduled_at.is_not(None),
                Post.scheduled_at <= now,
            ).limit(100)
        ).all()
        scheduled = missed = 0
        for post in rows:
            if _missed(post, now):
                _mark_missed(session, post)
                missed += 1
                continue
            jobs = find_publish_jobs(session, post.id)
            active = [j for j in jobs if j.status in (JobStatusEnum.queued, JobStatusEnum.running)]
            overdue = post.scheduled_at <= now - _td(minutes=LOST_QUEUED_MIN)
            if active and not (overdue and all(j.status == JobStatusEnum.queued for j in active)):
                continue
            # (a queued job that still hasn't started 15 min after the due time is lost;
            #  duplicates are harmless — whichever run comes second sees a non-queued post)
            for j in active:
                j.status = JobStatusEnum.error
                j.last_error = ((j.last_error or '') + ' lost_in_queue: re-enqueued').strip()
                session.add(j)
            payload = publish_payload_for(session, post.id)
            job = Job(kind=JobKindEnum.publish, payload_json=_json.dumps(payload, ensure_ascii=False), status=JobStatusEnum.queued)
            session.add(job)
            session.commit()
            session.refresh(job)
            publish_news_post.schedule(args=(job.id, post.id), delay=0)
            scheduled += 1
        return f"scheduled={scheduled} missed={missed} stuck={len(stuck)}"


def _short_edit(brief) -> dict:
    """Trim + overlays stored on a Shorts brief, validated, plus a file-name
    suffix that changes whenever they do ('' for an unedited clip)."""
    import hashlib, json
    from app.services.text_overlay import clean_overlays, clean_trim
    ts, te = clean_trim(getattr(brief, 'trim_start', None), getattr(brief, 'trim_end', None))
    try:
        raw = json.loads(getattr(brief, 'overlays_json', None) or '[]')
    except Exception:
        raw = []
    overlays = clean_overlays(raw, (te - (ts or 0)) if te else None)
    suffix = ''
    if ts or te or overlays:
        sig = json.dumps([ts, te, overlays], ensure_ascii=False, sort_keys=True)
        suffix = '_' + hashlib.sha1(sig.encode('utf-8')).hexdigest()[:8]
    return {'trim_start': ts or 0.0, 'trim_end': te or 0.0, 'overlays': overlays, 'suffix': suffix}


def _short_spec(brief, duration=None):
    """The video-editor spec stored on a Shorts brief (always 9:16), or None for a
    clip queued before the factory used the shared editor."""
    import json
    from app.services.video_edit import short_spec
    raw = getattr(brief, 'edit_spec_json', None)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return short_spec(data, duration) if isinstance(data, dict) else None


def _render_short_file(brief, src_abs: str, out_abs: str, music_abs, edit: dict) -> None:
    """Render one Shorts clip: overlay PNGs go to a temp folder that is removed
    afterwards, whatever happens."""
    import shutil, tempfile, os
    from app.services.video_editor import render_vertical_with_music
    from app.services.text_overlay import render_overlay_png
    tmp = tempfile.mkdtemp(prefix='ip_overlays_') if edit['overlays'] else None
    try:
        pngs = []
        for i, o in enumerate(edit['overlays']):
            path = os.path.join(tmp, f'ovl_{i}.png')
            render_overlay_png(o['text'], o['pos'], o['size'], out_path=path)
            pngs.append((path, o['start'], o['end']))
        render_vertical_with_music(
            src_abs, out_abs,
            crop_x_pct=(brief.crop_x if brief.crop_x is not None else 50) / 100.0,
            crop_y_pct=(brief.crop_y if brief.crop_y is not None else 50) / 100.0,
            music_path=music_abs,
            music_offset_s=float(brief.music_offset_sec or 0),
            fade_in_s=float(brief.fade_in_ms or 0) / 1000.0,
            fade_out_s=float(brief.fade_out_ms or 0) / 1000.0,
            start_time=edit['trim_start'],
            end_time=edit['trim_end'],
            zoom=float(brief.crop_scale or 1.0),
            overlays=pngs or None,
        )
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


@huey.task()
def render_short(job_id: int, brief_id: int) -> str:
    """Render a batched short (9:16 vertical + poster frame), then create the
    per-platform Posts and schedule their publish jobs.

    Posts are created *after* the render (not at submit time) so the per-minute
    enqueue_due_posts cron can never publish a not-yet-rendered clip.
    """
    from app.models.models import Brief, Post, Job, PostStatusEnum, JobKindEnum, JobStatusEnum, PlatformEnum
    from app.services.video_editor import extract_poster_frame
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
            spec = None
            if getattr(brief, 'edit_spec_json', None):
                from app.services import video_edit
                info = video_edit.probe(src_abs)
                spec = _short_spec(brief, info['duration'])
            if spec is not None:
                # Edited in the shared video editor: the same one-pass render as the
                # editor's own, so the clip looks exactly like its preview.
                from app.services import music_library
                if spec['music'] and not music_library.is_allowed(
                        spec['music']['path'], extra=music_library.preset_music_paths(session)):
                    logger.warning(f"render_short brief={brief_id}: track not found, rendering without music: "
                                   f"{spec['music']['path']}")
                    spec['music'] = None
                out_rel = f"{base}_vertical_{video_edit.spec_hash(spec)}.mp4"
            else:
                # Trim + text overlays go into the file name, so an edited clip never
                # reuses a stale render (plain clips keep the old `_vertical.mp4`).
                edit = _short_edit(brief)
                out_rel = f"{base}_vertical{edit['suffix']}.mp4"
            out_abs = os.path.abspath(os.path.join('media', out_rel))
            already = bool(vertical_rel) and os.path.basename(vertical_rel) == out_rel and os.path.exists(out_abs)
            if not already:
                if spec is not None:
                    video_edit.render_file(src_abs, out_abs, spec, info)
                else:
                    from app.core.paths import on_this_drive
                    music_abs = None
                    mp = on_this_drive(getattr(brief, 'music_path', None) or '')
                    if mp and os.path.exists(mp):
                        music_abs = mp
                    _render_short_file(brief, src_abs, out_abs, music_abs, edit)
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


def _has_numbers(m: dict) -> bool:
    return any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in m.values())


def _apply_metrics(post, metrics: dict) -> None:
    """Store a fetch result in post.metrics_json (the caller commits).

    A failed fetch must not wipe good numbers (this runs every 30 min): keep the
    previous counters and just attach the error. Errors are redacted like
    Post.error_text — they can quote API URLs. `checked_at` (UTC) drives the daily
    YouTube refresh.
    """
    import json
    from app.core.redact import redact
    metrics = dict(metrics or {})
    if metrics.get('error'):
        metrics['error'] = redact(str(metrics['error']))
    try:
        prev = json.loads(post.metrics_json or '{}') or {}
    except Exception:
        prev = {}
    if (metrics.get('error') or not _has_numbers(metrics)) and _has_numbers(prev):
        kept = {k: v for k, v in prev.items() if k not in ('error', 'stale', 'checked_at')}
        if metrics.get('error'):
            kept['error'] = metrics['error']
            kept['stale'] = True
        metrics = kept
    if metrics:
        metrics['checked_at'] = datetime.utcnow().isoformat(timespec='seconds')
    post.metrics_json = json.dumps(metrics, ensure_ascii=False)


@huey.task()
def refresh_metrics_for_post(post_id: int) -> str:
    from app.models.models import Post, PostStatusEnum
    with Session(engine) as session:
        post = session.get(Post, post_id)
        if not post:
            return "not_found"
        metrics = {}
        try:
            from app.platforms import METRICS, platform_key
            fetch = METRICS.get(platform_key(post.platform))
            if fetch is not None:
                metrics = fetch(post) or {}
        except Exception as exc:
            metrics = {"error": str(exc)}
        _apply_metrics(post, metrics)
        # Backstop: if a platform post id or url exists but status is still running, mark it done
        try:
            if post.status == PostStatusEnum.running and (post.platform_post_id or post.platform_url):
                post.status = PostStatusEnum.done
        except Exception:
            pass
        session.add(post)
        session.commit()
        return "ok"


METRICS_RECENT_DAYS = 7                   # refreshed on every refresh_recent_metrics run
YT_DAILY_INTERVAL = timedelta(hours=23)   # then ~daily; <24h so a PC that is only on
                                          # part of the day still gets one check a day


def youtube_posts_due(session, now=None) -> list:
    """Published YouTube posts whose stats are due: every run during the first week,
    then about once a day until `yt_metrics_days` after publish — Shorts keep
    picking up views for weeks."""
    import json
    from sqlmodel import select
    from app.models.models import Post, PostStatusEnum, PlatformEnum
    from app.services.settings import DEFAULTS, load_settings
    now = now or datetime.utcnow()
    try:
        days = int(load_settings().get('yt_metrics_days'))
    except (TypeError, ValueError):
        days = int(DEFAULTS['yt_metrics_days'])
    rows = session.exec(
        select(Post).where(
            Post.platform == PlatformEnum.yt,
            Post.status == PostStatusEnum.done,
            Post.platform_post_id.is_not(None),
            Post.published_at.is_not(None),
            Post.published_at >= now - timedelta(days=max(days, METRICS_RECENT_DAYS)),
        ).order_by(Post.published_at.desc())
    ).all()
    due = []
    for p in rows:
        if p.published_at >= now - timedelta(days=METRICS_RECENT_DAYS):
            due.append(p.id)
            continue
        try:
            checked = datetime.fromisoformat(json.loads(p.metrics_json or '{}').get('checked_at') or '')
        except (TypeError, ValueError, AttributeError):
            checked = None
        if checked is None or now - checked >= YT_DAILY_INTERVAL:
            due.append(p.id)
    return due


@huey.task()
def refresh_youtube_metrics(post_ids: list) -> str:
    """Stats for many YouTube posts at once: one videos.list call (1 quota unit) per
    50 videos instead of a call per post."""
    from app.models.models import Post
    from app.platforms.youtube import fetch_metrics_batch
    with Session(engine) as session:
        targets = [(p.id, p.platform_post_id.strip()) for p in (session.get(Post, pid) for pid in post_ids or [])
                   if p is not None and (p.platform_post_id or '').strip()]
    results = fetch_metrics_batch([vid for _, vid in targets])  # network outside the DB session
    failed = 0
    with Session(engine) as session:
        for pid, vid in targets:
            post = session.get(Post, pid)
            if post is None:
                continue
            metrics = results.get(vid) or {"error": "yt_no_result"}
            failed += bool(metrics.get('error'))
            _apply_metrics(post, metrics)
            session.add(post)
        session.commit()
    return f"videos={len(targets)} failed={failed}"


@huey.task()
def backfill_youtube_metrics() -> str:
    """Refresh every published YouTube video whatever its age — for posts that aged out
    of the schedule while the token was dead. Runs after each YouTube (re)connect."""
    from sqlmodel import select
    from app.models.models import Post, PostStatusEnum, PlatformEnum
    with Session(engine) as session:
        ids = list(session.exec(
            select(Post.id).where(
                Post.platform == PlatformEnum.yt,
                Post.status == PostStatusEnum.done,
                Post.platform_post_id.is_not(None),
            )
        ).all())
    return refresh_youtube_metrics.call_local(ids)


@huey.periodic_task(crontab(minute='*/30'))
def refresh_recent_metrics() -> str:
    """Every 30 minutes refresh metrics for posts published in the last 7 days,
    so Analytics stays fresh without manual clicking. YouTube goes out as one batched
    task and keeps a daily refresh for much longer (see youtube_posts_due)."""
    from app.models.models import Post, PostStatusEnum, PlatformEnum
    from sqlmodel import select
    cutoff = datetime.utcnow() - timedelta(days=METRICS_RECENT_DAYS)
    with Session(engine) as session:
        rows = session.exec(
            select(Post).where(
                Post.status == PostStatusEnum.done,
                # TikTok: tiktok_pipeline_tick reads it in batches and keeps a history
                Post.platform.not_in([PlatformEnum.yt, PlatformEnum.tiktok]),
                Post.published_at.is_not(None),
                Post.published_at >= cutoff,
            ).order_by(Post.published_at.desc()).limit(60)
        ).all()
        ids = [p.id for p in rows if modules.platform_on(p.platform)]
        yt_ids = youtube_posts_due(session) if modules.on('youtube') else []
    if yt_ids:
        refresh_youtube_metrics.schedule(args=(yt_ids,), delay=0)
    # Stagger requests to be gentle on platform APIs
    for i, pid in enumerate(ids):
        refresh_metrics_for_post.schedule(args=(pid,), delay=i * 2)
    return f"scheduled={len(ids)} youtube={len(yt_ids)}"


@huey.periodic_task(crontab(minute='*/15'))
def collect_marketing_metrics(force: bool = False) -> str:
    """Home-screen metrics: followers and publication stats time-boxed (this worker
    also publishes); Steam wishlists/sales go to a background thread."""
    from app.services.marketing_collect import collect_all
    return str(collect_all(force=force))


@huey.periodic_task(crontab(minute='*/10'))
def tiktok_pipeline_tick() -> str:
    """/tiktok: has a draft been posted in the app (status fetch), fresh views and
    likes of live clips (one batched video.query), factory drafts adopted."""
    if not modules.on('tiktok'):
        return 'module_off'
    from app.services import tiktok_pipeline
    with Session(engine) as session:
        return str(tiktok_pipeline.sync(session, in_worker=True))


@huey.periodic_task(crontab(minute='40', hour='*/6'))
def refresh_outreach_creators() -> str:
    """YouTube API data on /outreach must be refreshed (or dropped) within 30 days."""
    if not modules.on('outreach'):
        return 'module_off'
    from app.services.outreach_discovery import refresh_stale
    return refresh_stale()


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
