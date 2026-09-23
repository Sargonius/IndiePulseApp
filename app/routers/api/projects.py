"""Projects (game presets) and Shorts clip-type presets."""

import os
import time
from app.db.session import get_session
from app.models.models import Project
from app.models.models import now_ts
from fastapi import APIRouter
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


def _projects_list_html(request: Request, session: Session) -> HTMLResponse:
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    projects = [
        {
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
        }
        for p in rows
    ]
    return templates_api.TemplateResponse(
        "partials/projects_list.html",
        {"request": request, "projects": projects},
    )


@router.get("/projects")
def list_projects(session: Session = Depends(get_session)):
    rows = session.exec(
        select(Project).where(Project.user_id == 1).order_by(Project.sort_order, Project.id)
    ).all()
    return [
        {
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
        }
        for p in rows
    ]


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
