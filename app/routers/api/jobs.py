"""Jobs page actions: active list, cleanup, kill, remove, retry, metrics."""

from app.db.session import get_session
from fastapi import APIRouter
from fastapi import Depends
from fastapi import Form
from fastapi import Request
from fastapi.responses import HTMLResponse
from loguru import logger
from sqlmodel import Session
from sqlmodel import select
from typing import Optional
from app.routers.api.common import templates_api

router = APIRouter()

@router.get("/jobs/active")
def jobs_active(session: Session = Depends(get_session)):
    from app.models.models import Job, JobStatusEnum
    from sqlmodel import select
    rows = session.exec(
        select(Job).where(Job.status.in_([JobStatusEnum.queued, JobStatusEnum.running])).order_by(Job.created_at.desc()).limit(100)
    ).all()
    items = []
    for j in rows:
        items.append({
            "id": j.id,
            "kind": getattr(j.kind, 'value', str(j.kind)),
            "status": getattr(j.status, 'value', str(j.status)),
            "created_at": getattr(j, 'created_at', None).isoformat() if getattr(j, 'created_at', None) else None,
        })
    return {"count": len(items), "jobs": items}


@router.post("/jobs/cleanup_stuck")
def jobs_cleanup_stuck(minutes: int = 10, action: str = "mark_error", session: Session = Depends(get_session)):
    """Mark 'running' jobs older than N minutes as error or retry them.
    action in {mark_error, retry}
    """
    from app.models.models import Job, JobStatusEnum, Post, PostStatusEnum, JobKindEnum
    from sqlmodel import select
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.utcnow() - _td(minutes=max(1, minutes))
    rows = session.exec(
        select(Job).where(Job.status == JobStatusEnum.running).order_by(Job.created_at.asc()).limit(200)
    ).all()
    affected = []
    for j in rows:
        try:
            created = getattr(j, 'created_at', None)
            if not created or created > cutoff:
                continue
            # Extract post_id if present
            import json as _json
            post_id = None
            try:
                post_id = (_json.loads(j.payload_json or '{}') or {}).get('post_id')
            except Exception:
                post_id = None
            p = session.get(Post, post_id) if post_id else None
            if action == 'retry':
                # Mark job error and requeue a new job for the same post
                j.status = JobStatusEnum.error
                j.last_error = (j.last_error or '') + ' [auto-cleanup: timeout]'
                if p is not None and p.status == PostStatusEnum.running:
                    p.status = PostStatusEnum.queued
                session.add(j)
                if p is not None:
                    session.add(p)
                session.commit()
                # Create a new publish job
                if post_id:
                    nj = Job(kind=JobKindEnum.publish, payload_json=j.payload_json, status=JobStatusEnum.queued)
                    session.add(nj)
                    session.commit()
                    session.refresh(nj)
                    from app.jobs.tasks import publish_news_post
                    publish_news_post.schedule(args=(nj.id, post_id), delay=0)
                    affected.append({"old_job": j.id, "new_job": nj.id, "post_id": post_id})
            else:
                # mark_error
                j.status = JobStatusEnum.error
                j.last_error = (j.last_error or '') + ' [auto-cleanup: timeout]'
                if p is not None and p.status == PostStatusEnum.running:
                    p.status = PostStatusEnum.error
                    p.error_text = (p.error_text or '') + ' [auto-cleanup: timeout]'
                session.add(j)
                if p is not None:
                    session.add(p)
                session.commit()
                affected.append({"job": j.id, "post_id": post_id})
        except Exception as exc:
            try:
                logger.error(f"jobs_cleanup error for job={getattr(j,'id',None)}: {exc}")
            except Exception:
                pass
    return {"ok": True, "affected": affected}


@router.post("/jobs/kill")
def jobs_kill(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.models.models import Job, JobStatusEnum, Post, PostStatusEnum
    import json as _json
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    # mark job error and related post canceled
    job.status = JobStatusEnum.error
    job.last_error = (job.last_error or '') + ' [killed_by_user]'
    post_id = None
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if post_id:
        post = session.get(Post, post_id)
        if post and post.status in (PostStatusEnum.queued, PostStatusEnum.running):
            post.status = PostStatusEnum.canceled
            session.add(post)
    session.add(job)
    session.commit()
    # return refreshed jobs table
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    # Enrich jobs for previews/platform/thumbs
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


@router.post("/jobs/remove")
def jobs_remove(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.models.models import Job, Post, PostStatusEnum
    import json as _json
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    # Best-effort cancel related post if running/queued
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if post_id:
        post = session.get(Post, post_id)
        if post and post.status in (PostStatusEnum.queued, PostStatusEnum.running):
            post.status = PostStatusEnum.canceled
            session.add(post)
    session.delete(job)
    session.commit()
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


@router.post("/jobs/refresh_metrics")
def jobs_refresh_metrics(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    from app.models.models import Job
    import json as _json
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if post_id:
        from app.jobs.tasks import refresh_metrics_for_post
        refresh_metrics_for_post.schedule(args=(post_id,), delay=0)
    # return refreshed table (status may update shortly after)
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})


@router.post("/jobs/retry")
def jobs_retry(request: Request, id: Optional[int] = Form(None), id_q: Optional[int] = None, session: Session = Depends(get_session)):
    from app.models.models import Job, JobStatusEnum, JobKindEnum, Post
    import json as _json
    job_id = id or id_q
    if not job_id:
        return HTMLResponse("<span style='color:red'>missing_id</span>", status_code=400)
    job = session.get(Job, job_id)
    if not job:
        return HTMLResponse("<span style='color:red'>job_not_found</span>", status_code=404)
    # Only retry publish jobs
    if getattr(job, 'kind', None) != JobKindEnum.publish:
        return HTMLResponse("<span style='color:orange'>retry_only_for_publish</span>")
    # Extract post_id
    post_id = None
    try:
        post_id = (_json.loads(job.payload_json or '{}') or {}).get('post_id')
    except Exception:
        post_id = None
    if not post_id:
        return HTMLResponse("<span style='color:red'>no_post_in_payload</span>", status_code=400)
    # Reset the post back to queued, otherwise publish_news_post exits with
    # 'skipped_already_handled' and the new job hangs in queued forever.
    from app.models.models import PostStatusEnum as _PSE
    target_post = session.get(Post, post_id)
    do_schedule = True
    if target_post is None:
        do_schedule = False
    elif target_post.status == _PSE.done:
        # Already published — don't duplicate the post on an accidental retry
        do_schedule = False
    else:
        # an explicit retry also un-sticks a 'running' post (the task only starts queued ones)
        target_post.status = _PSE.queued
        target_post.error_text = None
        # Manual retry means "publish now", drop a stale schedule
        target_post.scheduled_at = None
        session.add(target_post)
        session.commit()
    if do_schedule:
        # Create a new queued job with same payload
        nj = Job(kind=JobKindEnum.publish, payload_json=job.payload_json, status=JobStatusEnum.queued)
        session.add(nj)
        session.commit()
        session.refresh(nj)
        from app.jobs.tasks import publish_news_post
        publish_news_post.schedule(args=(nj.id, post_id), delay=0)
        # The old failed job row is no longer actionable — mark it handled
        try:
            if job.status in (JobStatusEnum.queued, JobStatusEnum.error):
                job.status = JobStatusEnum.done
                job.last_error = ((job.last_error or '') + ' retried_as_job_' + str(nj.id)).strip()
                session.add(job)
                session.commit()
        except Exception:
            pass
    # refresh table
    from app.models.models import Job as _Job, Post as _Post, Brief as _Brief
    rows = session.exec(select(_Job).order_by(_Job.created_at.desc()).limit(50)).all()
    import json as _json
    enriched = []
    for j in rows:
        plat = ''
        preview = ''
        thumb_url = None
        try:
            payload = _json.loads(j.payload_json or '{}') if j.payload_json else {}
            pid = payload.get('post_id')
            if pid:
                p = session.get(_Post, pid)
                if p:
                    plat = (p.platform.value if hasattr(p.platform, 'value') else str(p.platform))
                    b = session.get(_Brief, getattr(p, 'brief_id', None))
                    if b:
                        base = (getattr(b, 'text_en', None) or getattr(b, 'text_ru', None) or '')
                        preview = (base[:140] + ('…' if len(base) > 140 else ''))
                        media_rel = getattr(b, 'media_path', None)
                        if media_rel:
                            name = str(media_rel).replace('\\','/').split('/')[-1]
                            thumb_url = f"/media/{name}"
        except Exception:
            pass
        enriched.append({
            'id': j.id,
            'kind': getattr(j, 'kind', None),
            'status': getattr(j, 'status', None),
            'attempt': getattr(j, 'attempt', 0),
            'last_error': getattr(j, 'last_error', None) or '',
            'created_at': getattr(j, 'created_at', None),
            'updated_at': getattr(j, 'updated_at', None),
            'platform': plat,
            'preview': preview,
            'thumb_url': thumb_url,
        })
    return templates_api.TemplateResponse('partials/jobs_table.html', {'request': request, 'jobs': enriched})
