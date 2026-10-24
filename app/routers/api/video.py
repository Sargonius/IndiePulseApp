"""Video rendering, preview frames, ffmpeg install and music library."""

import os
import time
from typing import Optional
from app.db.session import get_session
from app.services.draft_store import save_draft
from app.services.settings import load_settings
from app.services.video_editor import _ffmpeg_path
from app.services.video_editor import install_ffmpeg_win
from app.services.video_editor import render_vertical_with_music
from fastapi import APIRouter
from fastapi import Body
from fastapi import Depends
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Query
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
from sqlmodel import Session
from app.routers.api.common import _e

router = APIRouter()

@router.post("/video/preview_frame")
def video_preview_frame(
    video: UploadFile = File(...),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
):
    """Generate a preview frame showing the crop area"""
    os.makedirs('render', exist_ok=True)
    os.makedirs('media', exist_ok=True)
    
    base = str(int(time.time()))
    safe_video_name = video.filename.replace('\\','/').split('/')[-1]
    video_path = os.path.join('render', f"{base}_src_{safe_video_name}")
    
    with open(video_path, 'wb') as f:
        f.write(video.file.read())
    
    # Generate preview frame with crop overlay
    preview_path = os.path.join('media', f"{base}_preview.jpg")
    
    try:
        ffmpeg = _ffmpeg_path()
        
        # Extract a frame and add crop overlay
        cmd = [
            ffmpeg, "-y", "-i", video_path,
            "-vf", f"select=eq(n\\,0),scale=-2:480,drawbox=x=(iw-270)*{crop_x_pct}:y=(ih-480)*{crop_y_pct}:w=270:h=480:color=red@0.5:t=fill",
            "-frames:v", "1",
            preview_path
        ]
        
        import subprocess
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg preview failed: {proc.stderr[-500:]}")
        
        rel = preview_path.replace('\\','/')
        return {"preview_url": f"/media/{os.path.basename(rel)}"}
        
    except Exception as e:
        # Provide a helpful hint for missing ffmpeg
        return {"error": str(e), "hint": "FFmpeg is required. Click 'Install FFmpeg' on the News page to auto-install on Windows, or install from ffmpeg.org and add to PATH."}


@router.post("/video/render_vertical")
def video_render_vertical(
    video: UploadFile = File(...),
    music: UploadFile = File(None),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
    music_offset_s: float = Form(0.0),
    fade_in_s: float = Form(1.0),
    fade_out_s: float = Form(1.0),
    start_time: float = Form(0.0),
    end_time: float = Form(0.0),
    zoom: float = Form(1.0),
):
    os.makedirs('render', exist_ok=True)
    os.makedirs('media', exist_ok=True)
    # Save inputs
    base = str(int(time.time()))
    # Clean filename for safe file operations
    safe_video_name = video.filename.replace('\\','/').split('/')[-1]
    video_path = os.path.join('render', f"{base}_src_{safe_video_name}")
    with open(video_path, 'wb') as f:
        f.write(video.file.read())
    music_path = None
    if music is not None:
        safe_music_name = music.filename.replace('\\','/').split('/')[-1]
        music_path = os.path.join('render', f"{base}_music_{safe_music_name}")
        with open(music_path, 'wb') as f:
            f.write(music.file.read())
    # Write final output into media so it is web-accessible
    out_path = os.path.join('media', f"{base}_vertical.mp4")
    render_vertical_with_music(
        input_video=video_path,
        output_video=out_path,
        crop_x_pct=crop_x_pct,
        crop_y_pct=crop_y_pct,
        music_path=music_path,
        music_offset_s=music_offset_s,
        fade_in_s=fade_in_s,
        fade_out_s=fade_out_s,
        start_time=start_time,
        end_time=end_time,
        zoom=zoom,
    )
    # Return relative path for preview/download
    rel = out_path.replace('\\','/')
    rel_basename = os.path.basename(rel)
    # Save to draft as media_vertical_path
    save_draft({"media_vertical_path": rel_basename})
    return {"output_path": rel, "output_rel": rel_basename, "output_url": f"/media/{rel_basename}"}


@router.post("/video/render_horizontal")
def video_render_horizontal(
    video: UploadFile = File(...),
    music: UploadFile = File(None),
    resolution: str = Form("1080p"),
    crop_x_pct: float = Form(0.5),
    crop_y_pct: float = Form(0.5),
    music_offset_s: float = Form(0.0),
    fade_in_s: float = Form(1.0),
    fade_out_s: float = Form(1.0),
    start_time: float = Form(0.0),
    end_time: float = Form(0.0),
    zoom: float = Form(1.0),
):
    """Render horizontal 16:9 video with optional music and effects."""
    os.makedirs('render', exist_ok=True)
    os.makedirs('media', exist_ok=True)
    # Save inputs
    base = str(int(time.time()))
    safe_video_name = video.filename.replace('\\','/').split('/')[-1]
    video_path = os.path.join('render', f"{base}_src_{safe_video_name}")
    with open(video_path, 'wb') as f:
        f.write(video.file.read())
    music_path = None
    if music is not None:
        safe_music_name = music.filename.replace('\\','/').split('/')[-1]
        music_path = os.path.join('render', f"{base}_music_{safe_music_name}")
        with open(music_path, 'wb') as f:
            f.write(music.file.read())
    
    # Import the new function
    from app.services.video_editor import render_horizontal_with_music
    
    # Write final output into media so it is web-accessible
    res_suffix = resolution.replace('p', '') if resolution else '1080'
    out_path = os.path.join('media', f"{base}_horizontal_{res_suffix}p.mp4")
    render_horizontal_with_music(
        input_video=video_path,
        output_video=out_path,
        resolution=resolution,
        crop_x_pct=crop_x_pct,
        crop_y_pct=crop_y_pct,
        music_path=music_path,
        music_offset_s=music_offset_s,
        fade_in_s=fade_in_s,
        fade_out_s=fade_out_s,
        start_time=start_time,
        end_time=end_time,
        zoom=zoom,
    )
    # Return relative path for preview/download
    rel = out_path.replace('\\','/')
    rel_basename = os.path.basename(rel)
    # Save to draft as media_normal_path (for horizontal platforms)
    save_draft({"media_normal_path": rel_basename})
    return {"output_path": rel, "output_rel": rel_basename, "output_url": f"/media/{rel_basename}"}


@router.post("/video/install_ffmpeg")
def video_install_ffmpeg():
    try:
        # Try resolving; if not present, run installer for Windows
        try:
            path = _ffmpeg_path()
        except Exception:
            path = install_ffmpeg_win()
        return {"ok": True, "path": path}
    except Exception as exc:
        return HTMLResponse(f"<span style='color:red'>FFmpeg install failed: {_e(str(exc))}</span>", status_code=500)


# Shown when a video has no readable frame (file gone, ffmpeg missing)
_VIDEO_PLACEHOLDER = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 48 48">'
                      '<rect width="48" height="48" rx="6" fill="#e2e8f0"/>'
                      '<path d="M19 15v18l14-9z" fill="#64748b"/></svg>')


@router.get("/media/poster/{brief_id}")
def media_poster(brief_id: int, session: Session = Depends(get_session)):
    """Poster frame of a post's video for the post tables (grabbed on first request)."""
    from fastapi.responses import FileResponse, Response
    from app.models.models import Brief
    from app.services import thumbs
    brief = session.get(Brief, brief_id)
    path = thumbs.ensure_poster(brief) if brief is not None else None
    if path:
        return FileResponse(path, media_type="image/jpeg")
    return Response(content=_VIDEO_PLACEHOLDER, media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@router.get("/music/list")
def music_list():
    """Tracks from every music folder in Settings (subfolders included), with durations."""
    from app.services import music_library
    try:
        cfg = load_settings()
        dirs = [{"path": d, "exists": os.path.isdir(d)} for d in music_library.folders(cfg)]
        return {"ok": True, "tracks": music_library.list_tracks(cfg), "folders": dirs}
    except Exception as exc:
        return {"ok": False, "error": str(exc), "tracks": []}


@router.get("/music/file")
def music_file(path: str = Query(""), session: Session = Depends(get_session)):
    """Play a library track in the editor (only files from the music folders/presets)."""
    from fastapi.responses import FileResponse
    from app.services import music_library
    if not music_library.is_allowed(path, extra=music_library.preset_music_paths(session)):
        raise HTTPException(status_code=404, detail="not a library track")
    return FileResponse(path)


@router.get("/music/peaks")
def music_peaks(path: str = Query(""), session: Session = Depends(get_session)):
    """Waveform of a library track: {duration, peaks: [0..1]}."""
    from app.services import music_library
    if not music_library.is_allowed(path, extra=music_library.preset_music_paths(session)):
        raise HTTPException(status_code=404, detail="not a library track")
    try:
        return {"duration": music_library.duration(path), "peaks": music_library.peaks(path)}
    except Exception as exc:
        return {"duration": music_library.duration(path), "peaks": [], "error": str(exc)}


# ---- Video editor (static/video_editor.js), used wherever a video is attached --------
def _edit_error(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/video/probe")
def video_probe(rel: str = Query("")):
    """A media/ video: its size/length/frame, and — for a file this editor made — the
    source and settings it was made from (so the editor reopens with them)."""
    from app.services import video_edit as ve
    try:
        path = ve.media_abs(rel)
    except ve.EditError as exc:
        raise _edit_error(exc)
    rel = ve.media_rel(path)
    out = {"rel": rel, "url": f"/media/{rel}", **ve.probe(path), "edit": None}
    edit = ve.load_edit(rel)
    if edit:
        try:
            src = ve.media_abs(edit["src"])
            out["edit"] = {"src": edit["src"], "spec": edit.get("spec") or {}}
            out["source"] = {"rel": edit["src"], "url": f"/media/{edit['src']}", **ve.probe(src)}
        except ve.EditError:
            out["edit"] = None  # the source was deleted: edit the result itself
    if not out.get("source"):
        out["source"] = {k: out[k] for k in ("rel", "url", "duration", "width", "height", "has_audio", "fps", "size_mb")}
    return out


@router.post("/video/edit")
def video_edit_start(data: Optional[dict] = Body(None), session: Session = Depends(get_session)):
    """{src, spec} → a render job (polled at /video/edit/{id}); done at once when the
    same settings were rendered before or nothing changed."""
    from app.services import music_library, video_edit as ve
    data = data or {}
    extra = music_library.preset_music_paths(session)
    try:
        return ve.start_render(str(data.get("src") or ""), data.get("spec"),
                               music_ok=lambda p: music_library.is_allowed(p, extra=extra))
    except ve.EditError as exc:
        raise _edit_error(exc)


@router.get("/video/edit/{job_id}")
def video_edit_status(job_id: str):
    from app.services import video_edit as ve
    job = ve.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="render not found (server restarted?)")
    return job


@router.post("/video/edit/{job_id}/cancel")
def video_edit_cancel(job_id: str):
    from app.services import video_edit as ve
    return {"ok": ve.cancel_job(job_id)}


@router.post("/video/overlay_preview")
def video_overlay_preview(data: Optional[dict] = Body(None)):
    """The exact PNG the editor will burn in for one text line at a w×h frame."""
    from fastapi.responses import Response
    from app.services.text_overlay import MAX_TEXT_LEN, POSITIONS, SIZES, render_overlay_png
    data = data or {}
    text = str(data.get("text") or "").strip()[:MAX_TEXT_LEN]
    pos = data.get("pos") if data.get("pos") in POSITIONS else "middle"
    size = data.get("size") if data.get("size") in SIZES else "m"
    try:
        frame = (int(data.get("w") or 1080), int(data.get("h") or 1920))
    except (TypeError, ValueError):
        frame = (1080, 1920)
    try:
        png = render_overlay_png(text, pos, size, frame=frame)
    except RuntimeError as exc:  # no usable font
        raise HTTPException(status_code=500, detail=str(exc))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/video/fit")
def video_fit(rel: str = Query(""), platforms: str = Query("")):
    """Will this video go to each platform as is, be compressed on publish, or not fit?"""
    from app.services import video_edit as ve
    try:
        path = ve.media_abs(rel)
    except ve.EditError as exc:
        raise _edit_error(exc)
    return ve.fit_report(path, [p.strip() for p in platforms.split(",") if p.strip()])


@router.get("/music/scan")
def music_scan():
    """Alias for music/list - scans music directory and returns track list with metadata."""
    return music_list()


@router.get("/music/waveform")
def music_waveform(path: str = Query("")):
    try:
        import os
        from pydub import AudioSegment
        p = path
        if not p:
            return HTMLResponse("<span style='color:red'>missing path</span>", status_code=400)
        if not os.path.isfile(p):
            return HTMLResponse("<span style='color:red'>not a file</span>", status_code=404)
        # Load audio and compute naive RMS over buckets (fast)
        seg = AudioSegment.from_file(p)
        duration_ms = len(seg)
        buckets = 400  # number of samples for display
        step = max(1, duration_ms // buckets)
        samples = []
        for i in range(0, duration_ms, step):
            chunk = seg[i:i+step]
            samples.append(int(chunk.rms))
        # Normalize 0..1
        m = max(1, max(samples) if samples else 1)
        norm = [round(s / m, 4) for s in samples]
        return {"ok": True, "duration_ms": duration_ms, "samples": norm}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
