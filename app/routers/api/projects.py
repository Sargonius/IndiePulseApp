"""Projects (game presets) and Shorts clip-type presets."""

import os
import time
from app.db.session import get_session
from app.models.models import Project
from app.models.models import now_ts
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Request
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
from sqlmodel import Session
from sqlmodel import select
from typing import Optional
from app.routers.api.common import templates_api

router = APIRouter()

def _pi(v):
    """Parse an optional int from a form value ('' -> None)."""
    try:
        return int(v) if (v is not None and str(v).strip() != "") else None
    except Exception:
        return None


def _pf(v):
    """Parse an optional float from a form value ('' -> None)."""
    try:
        return float(v) if (v is not None and str(v).strip() != "") else None
    except Exception:
        return None


def _app_id(v):
    """Steam App ID from '1156500' or a store URL ('' -> None)."""
    import re as _re
    text = str(v or "").strip()
    m = _re.search(r"/app/(\d+)", text) or _re.fullmatch(r"(\d+)", text)
    return int(m.group(1)) if m else None


def _stage(v):
    from app.services.marketing import PROJECT_STAGES
    v = (v or "").strip()
    return v if v in PROJECT_STAGES else None


def _status(v):
    from app.services.marketing import PROJECT_STATUSES
    v = (v or "").strip()
    return v if v in PROJECT_STATUSES else None


def _project_dict(p) -> dict:
    from app.services.marketing import PROJECT_STAGES
    return {
        "id": p.id,
        "name": p.name,
        "icon_url": f"/media/{p.icon_path}" if p.icon_path else None,
        "tags_json": p.tags_json or "",
        "platforms_json": p.platforms_json or "{}",
        "sort_order": p.sort_order,
        "crop_x": p.crop_x,
        "crop_y": p.crop_y,
        "crop_scale": p.crop_scale,
        "music_path": p.music_path or "",
        "music_offset_sec": p.music_offset_sec,
        "fade_in_ms": p.fade_in_ms,
        "fade_out_ms": p.fade_out_ms,
        "platforms_csv": p.platforms_csv or "",
        "steam_app_id": p.steam_app_id,
        "stage": p.stage or "",
        "stage_label": PROJECT_STAGES.get(p.stage or "", ""),
        "status": p.status or "active",
    }


def _projects_list_html(request: Request, session: Session) -> HTMLResponse:
    from app.services.marketing import PROJECT_STAGES, PROJECT_STATUSES
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    return templates_api.TemplateResponse(
        "partials/projects_list.html",
        {"request": request, "projects": [_project_dict(p) for p in rows],
         "stages": PROJECT_STAGES, "statuses": PROJECT_STATUSES},
    )


@router.get("/projects")
def list_projects(session: Session = Depends(get_session)):
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    return [_project_dict(p) for p in rows]


# ---- Shorts-Factory "clip type" presets (title + desc + tags + music) --------
def _preset_dict(p) -> dict:
    return {
        "id": p.id,
        "name": p.name,
        "title": getattr(p, "title", None) or "",
        "text_en": getattr(p, "text_en", None) or "",
        "tags": p.tags_json or "",
        "music_path": getattr(p, "music_path", None) or "",
    }


@router.get("/shorts/presets")
def shorts_presets_list(session: Session = Depends(get_session)):
    from app.models.models import Preset
    rows = session.exec(
        select(Preset).where(Preset.user_id == 1).order_by(Preset.id)
    ).all()
    return [_preset_dict(p) for p in rows]


@router.post("/shorts/presets")
def shorts_presets_save(
    id: str = Form(""),
    name: str = Form(""),
    title: str = Form(""),
    text_en: str = Form(""),
    tags: str = Form(""),
    music_path: str = Form(""),
    session: Session = Depends(get_session),
):
    from app.models.models import Preset
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    try:
        pid = int(id) if str(id).strip() else None
    except Exception:
        pid = None
    obj = session.get(Preset, pid) if pid else None
    if obj is None:
        obj = Preset(user_id=1, name=name, tags_json="")
    obj.name = name
    obj.title = (title or "").strip()
    obj.text_en = (text_en or "").strip()
    obj.tags_json = (tags or "").strip()
    obj.music_path = (music_path or "").strip()
    session.add(obj)
    session.commit()
    session.refresh(obj)
    return _preset_dict(obj)


@router.delete("/shorts/presets/{preset_id}")
def shorts_presets_delete(preset_id: int, session: Session = Depends(get_session)):
    from app.models.models import Preset
    obj = session.get(Preset, preset_id)
    if obj is not None:
        session.delete(obj)
        session.commit()
    return {"ok": True}


# ---- Shorts Factory: a clip goes to media/ as soon as it is dropped ------------
@router.post("/shorts/upload")
def shorts_upload_clip(file: UploadFile = File(...)):
    """Save one dropped clip → {rel, url, duration, width, height, has_audio, ...}."""
    from app.services import shorts_upload, video_edit as ve
    rel = shorts_upload.save(file.file, file.filename or "clip.mp4")
    info = ve.probe(ve.media_abs(rel))
    if not info["width"] or not info["height"]:
        shorts_upload.remove(rel)
        raise HTTPException(status_code=400, detail="not a video: ffprobe found no picture")
    return {"rel": rel, "url": f"/media/{rel}", **info}


@router.delete("/shorts/upload")
def shorts_upload_delete(rel: str = "", session: Session = Depends(get_session)):
    """Drop a clip removed from the page before the submit (never one a brief uses)."""
    from app.services import shorts_upload
    return {"ok": shorts_upload.delete(session, rel)}


@router.post("/shorts/upload/discard")
def shorts_upload_discard(data: Optional[dict] = Body(None), session: Session = Depends(get_session)):
    """{rels: [...]} — the page was closed with clips that never went to the queue
    (sent with navigator.sendBeacon, which can only POST)."""
    from app.services import shorts_upload
    rels = (data or {}).get("rels")
    rels = [str(r) for r in rels[:200]] if isinstance(rels, list) else []
    return {"deleted": sum(1 for r in rels if shorts_upload.delete(session, r))}


# ---- Shorts Factory: where the queue ends (a new batch is scheduled after it) ---
@router.get("/shorts/queue_end")
def shorts_queue_end(session: Session = Depends(get_session)):
    """The last Short scheduled ahead → {last, title, queued}. Counts queued posts of
    factory briefs and clips still rendering (their time sits in the render job).
    Times are local and naive, like Post.scheduled_at; past ones are ignored."""
    import json as _json
    from datetime import datetime as _dt
    from app.models.models import Brief, Job, JobKindEnum, JobStatusEnum, Post, PostStatusEnum
    now = _dt.now()
    latest: dict[int, _dt] = {}  # brief_id → its latest planned publish

    def _note(bid, when):
        if when and when > now and (bid not in latest or when > latest[bid]):
            latest[bid] = when

    rows = session.exec(
        select(Post.brief_id, Post.scheduled_at)
        .join(Brief, Brief.id == Post.brief_id)
        .where(Brief.vertical_916 == True, Post.status == PostStatusEnum.queued,  # noqa: E712
               Post.scheduled_at.is_not(None))
    ).all()
    for bid, when in rows:
        _note(bid, when)
    jobs = session.exec(
        select(Job).where(Job.kind == JobKindEnum.render,
                          Job.status.in_([JobStatusEnum.queued, JobStatusEnum.running]))
    ).all()
    for j in jobs:
        try:
            payload = _json.loads(j.payload_json or "{}") or {}
            when = _dt.fromisoformat(payload["scheduled_at"]).replace(tzinfo=None)
            _note(int(payload["brief_id"]), when)
        except Exception:
            continue
    if not latest:
        return {"last": None, "title": "", "queued": 0}
    bid, last = max(latest.items(), key=lambda kv: kv[1])
    brief = session.get(Brief, bid)
    return {"last": last.replace(microsecond=0).isoformat(), "title": (brief.title if brief else "") or "",
            "queued": len(latest)}


# ---- Shorts editor: text overlay preview + checklist ---------------------------
@router.post("/shorts/overlay_preview")
def shorts_overlay_preview(data: Optional[dict] = Body(None)):
    """The exact 1080x1920 PNG that render_short will burn in for one overlay."""
    from fastapi.responses import Response
    from app.services.text_overlay import MAX_TEXT_LEN, POSITIONS, SIZES, render_overlay_png
    data = data or {}
    text = str(data.get("text") or "").strip()[:MAX_TEXT_LEN]
    pos = data.get("pos") if data.get("pos") in POSITIONS else "middle"
    size = data.get("size") if data.get("size") in SIZES else "m"
    try:
        png = render_overlay_png(text, pos, size)
    except RuntimeError as exc:  # no usable font
        raise HTTPException(status_code=500, detail=str(exc))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


def _recent_short_titles(session: Session, project_id, limit: int = 3) -> list[str]:
    """Titles of the last clips already published for this project (newest first)."""
    from app.models.models import Brief, Post, PostStatusEnum
    if not project_id:
        return []
    rows = session.exec(
        select(Brief.title, Post.published_at)
        .join(Post, Post.brief_id == Brief.id)
        .where(Brief.project_id == project_id, Post.status == PostStatusEnum.done,
               Brief.title.is_not(None))
        .order_by(Post.published_at.desc(), Post.id.desc())
    ).all()
    out: list[str] = []
    for title, _ in rows:
        if title and title not in out:
            out.append(title)
        if len(out) >= limit:
            break
    return out


@router.post("/shorts/lint")
def shorts_lint(data: Optional[dict] = Body(None), session: Session = Depends(get_session)):
    """Checklist for a whole batch: {project_id, goal_id, platforms, cards: [...]}
    → {cards: [{findings, counts}]} in the same order."""
    from app.services.shorts_lint import lint_batch
    data = data or {}
    pid = _pi(data.get("project_id"))
    project = session.get(Project, pid) if pid else None
    cards = data.get("cards") if isinstance(data.get("cards"), list) else []
    platforms = [str(p) for p in (data.get("platforms") or []) if p] if isinstance(data.get("platforms"), list) else []
    result = lint_batch(
        cards[:100],
        project_name=(project.name if project else ""),
        goal_id=data.get("goal_id"),
        platforms=platforms,
        recent_titles=_recent_short_titles(session, project.id if project else None),
        fade_in=data.get("fade_in"),
    )
    return {"cards": result, "project_name": project.name if project else ""}


# ---- Shorts: translation + repost to more networks ----------------------------
@router.post("/shorts/translate")
def shorts_translate(data: Optional[dict] = Body(None)):
    """{title, desc, tags, target: en|ja|ru} → the same fields translated (no side effects)."""
    from app.services import repost
    data = data or {}
    target = str(data.get("target") or "")
    if target not in repost.LANGS:
        raise HTTPException(status_code=400, detail="target must be one of en, ja, ru")
    try:
        return repost.translate(data, target)
    except repost.TranslateError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@router.post("/shorts/{brief_id}/repost")
def shorts_repost(brief_id: int, data: Optional[dict] = Body(None), session: Session = Depends(get_session)):
    """Queue a published short on more networks: {items: [{platform, lang, title, text,
    tags, text_ru, text_en}], allow_duplicate} → {created, skipped}."""
    from app.models.models import Brief
    from app.services import repost
    data = data or {}
    brief = session.get(Brief, brief_id)
    if brief is None or not brief.vertical_916:
        raise HTTPException(status_code=404, detail="short not found")
    items = data.get("items") if isinstance(data.get("items"), list) else []
    try:
        return repost.create_reposts(session, brief, items[:20], allow_duplicate=bool(data.get("allow_duplicate")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/projects/partial", response_class=HTMLResponse)
def projects_partial(request: Request, session: Session = Depends(get_session)):
    return _projects_list_html(request, session)


@router.post("/projects", response_class=HTMLResponse)
def create_project(
    request: Request,
    name: str = Form(...),
    tags_json: Optional[str] = Form(""),
    platforms_json: Optional[str] = Form("{}"),
    sort_order: Optional[int] = Form(0),
    crop_x: Optional[str] = Form(None),
    crop_y: Optional[str] = Form(None),
    crop_scale: Optional[str] = Form(None),
    music_path: Optional[str] = Form(""),
    music_offset_sec: Optional[str] = Form(None),
    fade_in_ms: Optional[str] = Form(None),
    fade_out_ms: Optional[str] = Form(None),
    platforms_csv: Optional[str] = Form(""),
    steam_app_id: Optional[str] = Form(""),
    stage: Optional[str] = Form(""),
    status: Optional[str] = Form(""),
    icon: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
):
    icon_path = None
    if icon and icon.filename:
        os.makedirs("media", exist_ok=True)
        safe_name = icon.filename.replace("..", "").replace("/", "_").replace("\\", "_")
        rel = f"media/{int(time.time())}_{safe_name}"
        with open(rel, "wb") as f:
            f.write(icon.file.read())
        icon_path = rel.split("media/", 1)[-1]
    p = Project(
        user_id=1,
        name=(name or "").strip(),
        icon_path=icon_path,
        tags_json=(tags_json or "").strip(),
        platforms_json=(platforms_json or "{}").strip(),
        sort_order=sort_order or 0,
        crop_x=_pi(crop_x),
        crop_y=_pi(crop_y),
        crop_scale=_pf(crop_scale),
        music_path=(music_path or "").strip() or None,
        music_offset_sec=_pi(music_offset_sec),
        fade_in_ms=_pi(fade_in_ms),
        fade_out_ms=_pi(fade_out_ms),
        platforms_csv=(platforms_csv or "").strip() or None,
        steam_app_id=_app_id(steam_app_id),
        stage=_stage(stage),
        status=_status(status),
    )
    session.add(p)
    session.commit()
    return _projects_list_html(request, session)


@router.post("/projects/{project_id}/update", response_class=HTMLResponse)
def update_project(
    project_id: int,
    request: Request,
    name: Optional[str] = Form(None),
    tags_json: Optional[str] = Form(None),
    platforms_json: Optional[str] = Form(None),
    sort_order: Optional[int] = Form(None),
    crop_x: Optional[str] = Form(None),
    crop_y: Optional[str] = Form(None),
    crop_scale: Optional[str] = Form(None),
    music_path: Optional[str] = Form(None),
    music_offset_sec: Optional[str] = Form(None),
    fade_in_ms: Optional[str] = Form(None),
    fade_out_ms: Optional[str] = Form(None),
    platforms_csv: Optional[str] = Form(None),
    steam_app_id: Optional[str] = Form(None),
    stage: Optional[str] = Form(None),
    status: Optional[str] = Form(None),
    icon: Optional[UploadFile] = File(None),
    session: Session = Depends(get_session),
):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    if name is not None:
        p.name = name.strip()
    if tags_json is not None:
        p.tags_json = tags_json.strip()
    if platforms_json is not None:
        p.platforms_json = platforms_json.strip()
    if sort_order is not None:
        p.sort_order = sort_order
    if crop_x is not None:
        p.crop_x = _pi(crop_x)
    if crop_y is not None:
        p.crop_y = _pi(crop_y)
    if crop_scale is not None:
        p.crop_scale = _pf(crop_scale)
    if music_path is not None:
        p.music_path = (music_path or "").strip() or None
    if music_offset_sec is not None:
        p.music_offset_sec = _pi(music_offset_sec)
    if fade_in_ms is not None:
        p.fade_in_ms = _pi(fade_in_ms)
    if fade_out_ms is not None:
        p.fade_out_ms = _pi(fade_out_ms)
    if platforms_csv is not None:
        p.platforms_csv = (platforms_csv or "").strip() or None
    if steam_app_id is not None:
        p.steam_app_id = _app_id(steam_app_id)
    if stage is not None:
        p.stage = _stage(stage)
    if status is not None:
        p.status = _status(status)
    if icon and icon.filename:
        os.makedirs("media", exist_ok=True)
        safe_name = icon.filename.replace("..", "").replace("/", "_").replace("\\", "_")
        rel = f"media/{int(time.time())}_{safe_name}"
        with open(rel, "wb") as f:
            f.write(icon.file.read())
        p.icon_path = rel.split("media/", 1)[-1]
    p.updated_at = now_ts()
    session.add(p)
    session.commit()
    return _projects_list_html(request, session)


@router.post("/projects/{project_id}/delete", response_class=HTMLResponse)
def delete_project(
    project_id: int,
    request: Request,
    session: Session = Depends(get_session),
):
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(status_code=404, detail="Project not found")
    session.delete(p)
    session.commit()
    return _projects_list_html(request, session)
