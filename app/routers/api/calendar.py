"""Calendar feed and drag-to-reschedule."""

from app.db.session import get_session
from fastapi import APIRouter
from fastapi import Depends
from pydantic import BaseModel
from sqlmodel import Session
from typing import List
from typing import Optional
from app.services import thumbs

router = APIRouter()

@router.get("/calendar/events")
def calendar_events(start: Optional[str] = None, end: Optional[str] = None, session: Session = Depends(get_session)):
    """Get grouped posts as FullCalendar events within the requested date range."""
    from sqlmodel import select
    from sqlmodel.sql.expression import or_
    from datetime import datetime as _dt
    from app.models.models import Post as _Post, Brief as _Brief
    
    query = select(_Post)
    if start and end:
        try:
            start_dt = _dt.fromisoformat(start.replace('Z', '').split('+')[0].split('.')[0])
            end_dt = _dt.fromisoformat(end.replace('Z', '').split('+')[0].split('.')[0])
            query = query.where(or_(
                _Post.scheduled_at.between(start_dt, end_dt),
                _Post.published_at.between(start_dt, end_dt),
                _Post.created_at.between(start_dt, end_dt),
            ))
        except Exception:
            pass
    rows = session.exec(query.order_by(_Post.created_at.desc()).limit(500)).all()
    
    groups = {}
    for p in rows:
        key = p.brief_id or f"post_{p.id}"
        if key not in groups:
            groups[key] = {'posts': [], 'brief': None}
        groups[key]['posts'].append(p)
    
    events = []
    for key, bucket in groups.items():
        posts = bucket['posts']
        brief_id = posts[0].brief_id
        brief = session.get(_Brief, brief_id) if brief_id else None
        
        preview = ''
        thumb_url = None
        brief_type = ''
        if brief:
            base = (getattr(brief, 'text_en', None) or getattr(brief, 'text_ru', None) or '')
            preview = base[:120] + ('...' if len(base) > 120 else '')
            thumb_url = thumbs.thumb_url(brief)
            brief_type = getattr(brief, 'type', '') or ''
        
        platform_items = []
        statuses = []
        dates = []
        for p in posts:
            platform = p.platform.value if hasattr(p.platform, 'value') else str(p.platform)
            status = p.status.value if hasattr(p.status, 'value') else str(p.status)
            statuses.append(status)
            event_date = p.scheduled_at or p.published_at or p.created_at
            if event_date:
                dates.append(event_date)
            platform_items.append({
                'platform': platform,
                'status': status,
                'post_id': p.id,
                'platform_url': p.platform_url,
            })
        
        # Group status: error > running > queued > done
        if any(s == 'error' for s in statuses):
            group_status = 'error'
        elif any(s == 'running' for s in statuses):
            group_status = 'running'
        elif any(s == 'queued' for s in statuses):
            group_status = 'queued'
        elif all(s == 'done' for s in statuses):
            group_status = 'done'
        else:
            group_status = 'queued'
        
        # Event date: earliest meaningful date in group
        event_date = min(dates) if dates else None
        if not event_date:
            continue
        
        type_label = 'Post'
        if str(brief_type) == 'video':
            type_label = 'Video'
        elif str(brief_type) == 'news':
            type_label = 'Post'
        
        platforms_label = ','.join(sorted({x['platform'] for x in platform_items}))
        title = f"{type_label} ({len(platform_items)}): {preview[:40]}{'...' if len(preview) > 40 else ''}"
        
        events.append({
            'id': f"brief_{brief_id or key}",
            'title': title,
            'start': event_date.isoformat(),
            'allDay': False,
            'startEditable': group_status == 'queued',
            'extendedProps': {
                'brief_id': brief_id,
                'status': group_status,
                'preview': preview,
                'thumb_url': thumb_url,
                'platforms': platform_items,
                'platforms_label': platforms_label,
                'type_label': type_label,
                'post_ids': [x['post_id'] for x in platform_items],
            }
        })
    
    return {'events': events}


class CalendarMoveRequest(BaseModel):
    post_id: Optional[int] = None
    brief_id: Optional[int] = None
    post_ids: Optional[List[int]] = None
    new_date: str


@router.post("/calendar/move")
def calendar_move(req: CalendarMoveRequest, session: Session = Depends(get_session)):
    """Move a group (brief) or single post to a new date (drag-drop from calendar)."""
    from datetime import datetime as _dt
    from app.models.models import Post, PostStatusEnum
    from sqlmodel import select
    
    # Parse new date
    try:
        date_str = req.new_date.replace('Z', '').split('+')[0].split('.')[0]
        new_dt = _dt.fromisoformat(date_str)
    except Exception as e:
        return {'ok': False, 'error': f'Invalid date format: {e}'}
    
    # Update group if brief_id is provided
    if req.brief_id:
        posts = session.exec(
            select(Post).where(Post.brief_id == req.brief_id, Post.status == PostStatusEnum.queued)
        ).all()
        if not posts:
            return {'ok': False, 'error': 'No queued posts in this group'}
        for p in posts:
            p.scheduled_at = new_dt
            session.add(p)
        session.commit()
        return {'ok': True}
    
    # Update list of post_ids if provided
    if req.post_ids:
        posts = session.exec(
            select(Post).where(Post.id.in_(req.post_ids), Post.status == PostStatusEnum.queued)
        ).all()
        if not posts:
            return {'ok': False, 'error': 'No queued posts found'}
        for p in posts:
            p.scheduled_at = new_dt
            session.add(p)
        session.commit()
        return {'ok': True}
    
    # Fallback to single post_id
    if not req.post_id:
        return {'ok': False, 'error': 'Missing post_id'}
    post = session.get(Post, req.post_id)
    if not post:
        return {'ok': False, 'error': 'Post not found'}
    if post.status != PostStatusEnum.queued:
        return {'ok': False, 'error': 'Can only move queued posts'}
    
    # Update post
    post.scheduled_at = new_dt
    session.add(post)
    session.commit()
    
    return {'ok': True}
