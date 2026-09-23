from datetime import datetime
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
        result = publisher(session, post, job)
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
        scheduled = 0
        for post in rows:
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
        return f"scheduled={scheduled} stuck={len(stuck)}"


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
    import json
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
        # A failed fetch must not wipe good numbers (this runs every 30 min): keep the
        # previous counters and just attach the error.
        try:
            prev = json.loads(post.metrics_json or '{}') or {}
        except Exception:
            prev = {}

        def _has_numbers(m: dict) -> bool:
            return any(isinstance(v, (int, float)) and not isinstance(v, bool) for v in m.values())

        if (metrics.get('error') or not _has_numbers(metrics)) and _has_numbers(prev):
            kept = {k: v for k, v in prev.items() if k not in ('error', 'stale')}
            if metrics.get('error'):
                kept['error'] = metrics['error']
                kept['stale'] = True
            metrics = kept
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
