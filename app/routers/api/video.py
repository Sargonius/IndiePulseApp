"""Video rendering, preview frames, ffmpeg install and music library."""

import os
import time
from app.services.draft_store import save_draft
from app.services.settings import load_settings
from app.services.video_editor import _ffmpeg_path
from app.services.video_editor import install_ffmpeg_win
from app.services.video_editor import render_vertical_with_music
from fastapi import APIRouter
from fastapi import File
from fastapi import Form
from fastapi import Query
from fastapi import UploadFile
from fastapi.responses import HTMLResponse
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


@router.get("/music/list")
def music_list():
    """List all music files in configured music directory with metadata."""
    try:
        cfg = load_settings()
        base = (cfg.get("music_dir") or "").strip()
        if not base:
            return {"ok": True, "tracks": []}
        import os
        if not os.path.isdir(base):
            return {"ok": True, "tracks": []}
        exts = {'.mp3', '.wav', '.flac', '.ogg', '.m4a', '.aac', '.wma'}
        items = []
        for name in sorted(os.listdir(base)):
            p = os.path.join(base, name)
            if not os.path.isfile(p):
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext in exts:
                # Get duration using pydub
                try:
                    from pydub import AudioSegment
                    seg = AudioSegment.from_file(p)
                    duration_ms = len(seg)
                    duration_sec = duration_ms / 1000.0
                except Exception:
                    duration_ms = 0
                    duration_sec = 0.0
                
                items.append({
                    "name": name,
                    "path": p,
                    "duration_ms": duration_ms,
                    "duration_sec": round(duration_sec, 2),
                })
        return {"ok": True, "tracks": items}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


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
