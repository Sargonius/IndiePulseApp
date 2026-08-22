from __future__ import annotations

import os
import subprocess
from typing import Optional


def _ffmpeg_path() -> str:
    # Prefer bundled ffmpeg if present
    local = os.path.join("vendor", "ffmpeg", "ffmpeg.exe")
    if os.name == "nt" and os.path.exists(local):
        return local
    
    # Check if ffmpeg is in PATH
    try:
        import shutil
        if shutil.which("ffmpeg"):
            return "ffmpeg"
    except:
        pass
    
    # Common Windows installation paths
    common_paths = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
    ]
    
    for path in common_paths:
        if os.path.exists(path):
            return path

    # Attempt auto-download on Windows
    if os.name == "nt":
        try:
            os.makedirs(os.path.join("vendor", "ffmpeg"), exist_ok=True)
            zip_path = os.path.join("vendor", "ffmpeg", "ffmpeg_win64.zip")
            # Try a list of known mirrors (first working one will be used)
            urls = [
                # BtbN GitHub builds (latest nightly)
                "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
                # Gyan.dev release essentials (versioned; may change over time)
                "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
            ]
            import httpx, zipfile, io, shutil
            data: Optional[bytes] = None
            last_err: Optional[Exception] = None
            for u in urls:
                try:
                    with httpx.Client(timeout=30.0) as c:
                        r = c.get(u)
                        r.raise_for_status()
                        data = r.content
                        break
                except Exception as e:
                    last_err = e
                    data = None
                    continue
            if data is None:
                raise RuntimeError(f"failed_to_download_ffmpeg: {last_err}")
            with open(zip_path, "wb") as f:
                f.write(data)
            # Extract and locate ffmpeg.exe and ffprobe.exe
            with zipfile.ZipFile(zip_path, 'r') as z:
                members = z.namelist()
                # Try to find bin/ffmpeg.exe within the archive
                ffmpeg_member = next((m for m in members if m.lower().endswith("/bin/ffmpeg.exe")), None)
                ffprobe_member = next((m for m in members if m.lower().endswith("/bin/ffprobe.exe")), None)
                # Some archives may store at root
                if not ffmpeg_member:
                    ffmpeg_member = next((m for m in members if m.lower().endswith("ffmpeg.exe")), None)
                if not ffprobe_member:
                    ffprobe_member = next((m for m in members if m.lower().endswith("ffprobe.exe")), None)
                if not ffmpeg_member:
                    raise RuntimeError("ffmpeg_exe_not_found_in_archive")
                # Extract to temp dir and copy
                temp_dir = os.path.join("vendor", "ffmpeg", "_tmp")
                os.makedirs(temp_dir, exist_ok=True)
                z.extract(ffmpeg_member, temp_dir)
                if ffprobe_member:
                    z.extract(ffprobe_member, temp_dir)
                # Compute extracted paths
                ffmpeg_src = os.path.join(temp_dir, ffmpeg_member)
                ffprobe_src = os.path.join(temp_dir, ffprobe_member) if ffprobe_member else None
                # Normalize separators
                ffmpeg_src = ffmpeg_src.replace("/", os.sep)
                if ffprobe_src:
                    ffprobe_src = ffprobe_src.replace("/", os.sep)
                dest_dir = os.path.join("vendor", "ffmpeg")
                ffmpeg_dest = os.path.join(dest_dir, "ffmpeg.exe")
                ffprobe_dest = os.path.join(dest_dir, "ffprobe.exe")
                shutil.copy2(ffmpeg_src, ffmpeg_dest)
                if ffprobe_src and os.path.exists(ffprobe_src):
                    shutil.copy2(ffprobe_src, ffprobe_dest)
                # Cleanup temp and zip
                try:
                    shutil.rmtree(temp_dir, ignore_errors=True)
                except Exception:
                    pass
                try:
                    os.remove(zip_path)
                except Exception:
                    pass
                if os.path.exists(ffmpeg_dest):
                    return ffmpeg_dest
        except Exception as e:
            # Fall through to error with a helpful message
            raise RuntimeError(
                "FFmpeg not found and auto-download failed. Please install FFmpeg and add it to PATH, "
                "or place ffmpeg.exe in vendor/ffmpeg/. "
                f"Details: {e}"
            )

    raise RuntimeError(
        "FFmpeg not found. Please install FFmpeg and add it to PATH, "
        "or place ffmpeg.exe in vendor/ffmpeg/ directory. "
        "Download from: https://ffmpeg.org/download.html"
    )


def install_ffmpeg_win() -> str:
    """Attempt to download and install FFmpeg into vendor/ffmpeg on Windows.
    Returns the path to ffmpeg.exe on success, raises RuntimeError on failure.
    """
    if os.name != "nt":
        raise RuntimeError("auto-install supported on Windows only")

    dest_dir = os.path.join("vendor", "ffmpeg")
    os.makedirs(dest_dir, exist_ok=True)
    zip_path = os.path.join(dest_dir, "ffmpeg_win64.zip")

    urls = [
        "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
    ]

    last_err: Optional[Exception] = None
    data: Optional[bytes] = None
    # First try Python httpx (works if TLS/mirror ok)
    try:
        import httpx
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IndiePulse/1.0"}
        for u in urls:
            try:
                with httpx.Client(timeout=40.0, follow_redirects=True, headers=headers) as c:
                    r = c.get(u)
                    r.raise_for_status()
                    data = r.content
                    break
            except Exception as e:
                last_err = e
                data = None
                continue
        if data is not None:
            with open(zip_path, "wb") as f:
                f.write(data)
    except Exception as e:
        last_err = e
        data = None

    # Fallback: PowerShell Invoke-WebRequest (handles some TLS/cert configs better)
    if data is None:
        import subprocess
        for u in urls:
            try:
                cmd = [
                    "powershell", "-NoProfile", "-Command",
                    f"Invoke-WebRequest -UseBasicParsing -Uri '{u}' -OutFile '{zip_path}'"
                ]
                proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                if proc.returncode == 0 and os.path.exists(zip_path) and os.path.getsize(zip_path) > 0:
                    break
            except Exception as e2:
                last_err = e2
        if not (os.path.exists(zip_path) and os.path.getsize(zip_path) > 0):
            raise RuntimeError(f"download_failed: {last_err}")

    # Extract and place binaries
    import zipfile, shutil
    with zipfile.ZipFile(zip_path, 'r') as z:
        members = z.namelist()
        ffmpeg_member = next((m for m in members if m.lower().endswith("/bin/ffmpeg.exe")), None)
        ffprobe_member = next((m for m in members if m.lower().endswith("/bin/ffprobe.exe")), None)
        if not ffmpeg_member:
            ffmpeg_member = next((m for m in members if m.lower().endswith("ffmpeg.exe")), None)
        if not ffprobe_member:
            ffprobe_member = next((m for m in members if m.lower().endswith("ffprobe.exe")), None)
        if not ffmpeg_member:
            raise RuntimeError("ffmpeg_exe_not_found_in_archive")
        temp_dir = os.path.join(dest_dir, "_tmp")
        os.makedirs(temp_dir, exist_ok=True)
        z.extract(ffmpeg_member, temp_dir)
        if ffprobe_member:
            z.extract(ffprobe_member, temp_dir)
        ffmpeg_src = os.path.join(temp_dir, ffmpeg_member).replace("/", os.sep)
        ffprobe_src = os.path.join(temp_dir, ffprobe_member).replace("/", os.sep) if ffprobe_member else None
        ffmpeg_dest = os.path.join(dest_dir, "ffmpeg.exe")
        ffprobe_dest = os.path.join(dest_dir, "ffprobe.exe")
        shutil.copy2(ffmpeg_src, ffmpeg_dest)
        if ffprobe_src and os.path.exists(ffprobe_src):
            shutil.copy2(ffprobe_src, ffprobe_dest)
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass
    try:
        os.remove(zip_path)
    except Exception:
        pass
    if not os.path.exists(ffmpeg_dest):
        raise RuntimeError("ffmpeg_install_failed")
    return ffmpeg_dest


def _probe_video_info(video_path: str) -> dict:
    """Probe video file and return info (duration, bitrate, resolution)."""
    try:
        import subprocess, json, os as _os
        ffmpeg = _ffmpeg_path()
        if _os.path.sep in ffmpeg or ("/" in ffmpeg) or ("\\" in ffmpeg):
            base_dir = _os.path.dirname(ffmpeg)
            exe = "ffprobe.exe" if _os.name == "nt" else "ffprobe"
            ffprobe = _os.path.join(base_dir, exe)
        else:
            ffprobe = "ffprobe"
        
        args = [
            ffprobe,
            "-v", "error",
            "-show_entries", "format=duration,bit_rate:stream=width,height,codec_type",
            "-of", "json",
            video_path,
        ]
        out = subprocess.check_output(args, stderr=subprocess.STDOUT, text=True)
        data = json.loads(out)
        
        # Extract info
        format_info = data.get("format", {})
        duration = float(format_info.get("duration", 0))
        bitrate = int(format_info.get("bit_rate", 0))
        
        # Find video stream
        width, height = 0, 0
        for stream in data.get("streams", []):
            if stream.get("codec_type") == "video":
                width = int(stream.get("width", 0))
                height = int(stream.get("height", 0))
                break
        
        return {
            "duration": duration,
            "bitrate": bitrate,
            "width": width,
            "height": height,
        }
    except Exception:
        return {"duration": 0, "bitrate": 0, "width": 0, "height": 0}


def compress_video_to_size(
    input_path: str,
    output_path: str,
    max_size_mb: int,
    resolution_fallback: bool = True
) -> bool:
    """
    Compress video to fit within max_size_mb.
    Strategy: reduce bitrate first, then resolution if still too large.
    Returns True if successful, False if failed to compress below limit.
    """
    ffmpeg = _ffmpeg_path()
    
    # Check current size
    current_size_mb = os.path.getsize(input_path) / (1024 * 1024)
    if current_size_mb <= max_size_mb:
        # Already small enough, just copy
        import shutil
        shutil.copy2(input_path, output_path)
        return True
    
    # Probe video info
    info = _probe_video_info(input_path)
    duration = info["duration"]
    if duration <= 0:
        return False
    
    # Calculate target bitrate (90% of theoretical max to leave room for overhead)
    target_bitrate_kbps = int((max_size_mb * 8 * 1024) / duration * 0.9)
    
    # Try 1: Reduce bitrate, keep resolution
    try:
        cmd = [
            ffmpeg, "-y", "-i", input_path,
            "-c:v", "libx264",
            "-b:v", f"{target_bitrate_kbps}k",
            "-maxrate", f"{target_bitrate_kbps}k",
            "-bufsize", f"{target_bitrate_kbps * 2}k",
            "-preset", "veryfast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", "128k",
            output_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0:
            output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
            if output_size_mb <= max_size_mb:
                return True
    except Exception:
        pass
    
    # Try 2: Reduce to 720p if resolution_fallback enabled
    if resolution_fallback and info["height"] > 720:
        try:
            # Re-calculate bitrate for 720p
            target_bitrate_kbps = int((max_size_mb * 8 * 1024) / duration * 0.85)
            cmd = [
                ffmpeg, "-y", "-i", input_path,
                "-vf", "scale=-2:720",
                "-c:v", "libx264",
                "-b:v", f"{target_bitrate_kbps}k",
                "-maxrate", f"{target_bitrate_kbps}k",
                "-bufsize", f"{target_bitrate_kbps * 2}k",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", "96k",
                output_path,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode == 0:
                output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                if output_size_mb <= max_size_mb:
                    return True
        except Exception:
            pass
    
    # Try 3: Reduce to 480p as last resort
    if resolution_fallback and info["height"] > 480:
        try:
            target_bitrate_kbps = int((max_size_mb * 8 * 1024) / duration * 0.80)
            cmd = [
                ffmpeg, "-y", "-i", input_path,
                "-vf", "scale=-2:480",
                "-c:v", "libx264",
                "-b:v", f"{target_bitrate_kbps}k",
                "-maxrate", f"{target_bitrate_kbps}k",
                "-bufsize", f"{target_bitrate_kbps * 2}k",
                "-preset", "veryfast",
                "-pix_fmt", "yuv420p",
                "-movflags", "+faststart",
                "-c:a", "aac",
                "-b:a", "64k",
                output_path,
            ]
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            if proc.returncode == 0:
                output_size_mb = os.path.getsize(output_path) / (1024 * 1024)
                if output_size_mb <= max_size_mb:
                    return True
        except Exception:
            pass
    
    return False


def ensure_video_size_limit(
    video_path: str,
    platform: str,
    settings: dict
) -> str:
    """
    Ensure video fits platform size limits. Compresses if needed.
    Returns path to video (original or compressed).
    """
    # Platform size limits in MB
    limits = {
        'discord': int(settings.get('discord_max_upload_mb', 50)),
        'telegram': 50,
        'tg': 50,
        'vk': 200,
        'x': 512,
        'reddit': 1024,
        'tiktok': 287,
        'yt': 256,  # For Shorts
        'fb_ru': 1024,
        'fb_en': 1024,
    }
    
    max_size_mb = limits.get(platform, 100)
    current_size_mb = os.path.getsize(video_path) / (1024 * 1024)
    
    if current_size_mb <= max_size_mb:
        return video_path
    
    # Need to compress
    base_name = os.path.basename(video_path)
    name_no_ext = os.path.splitext(base_name)[0]
    ext = os.path.splitext(base_name)[1]
    compressed_path = os.path.join(
        os.path.dirname(video_path),
        f"{name_no_ext}_compressed{ext}"
    )
    
    success = compress_video_to_size(video_path, compressed_path, max_size_mb, resolution_fallback=True)
    
    if success and os.path.exists(compressed_path):
        return compressed_path
    else:
        # Compression failed, return original and let caller handle error
        return video_path


def render_horizontal_with_music(
    input_video: str,
    output_video: str,
    resolution: str = "1080p",
    crop_x_pct: float = 0.5,
    crop_y_pct: float = 0.5,
    music_path: Optional[str] = None,
    music_offset_s: float = 0.0,
    fade_in_s: float = 1.0,
    fade_out_s: float = 1.0,
    start_time: float = 0.0,
    end_time: float = 0.0,
    zoom: float = 1.0,
) -> None:
    """
    Render a horizontal 16:9 video from any source.
    Resolution: "1080p" (1920x1080) or "720p" (1280x720).
    Steps: scale to target height, crop to 16:9 using provided offsets, optional music mix with fades.
    """
    ffmpeg = _ffmpeg_path()
    
    # Determine target resolution
    if resolution == "720p":
        target_w, target_h = 1280, 720
    else:  # default to 1080p
        target_w, target_h = 1920, 1080
    
    def _probe_duration_seconds(path: str) -> float:
        """Return media duration in seconds using ffprobe; 0.0 on failure."""
        try:
            import subprocess, json, os as _os
            if _os.path.sep in ffmpeg or ("/" in ffmpeg) or ("\\" in ffmpeg):
                base_dir = _os.path.dirname(ffmpeg)
                exe = "ffprobe.exe" if _os.name == "nt" else "ffprobe"
                ffprobe = _os.path.join(base_dir, exe)
            else:
                ffprobe = "ffprobe"
            args = [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1",
                path,
            ]
            out = subprocess.check_output(args, stderr=subprocess.STDOUT, text=True).strip()
            return float(out)
        except Exception:
            return 0.0

    def _probe_has_audio(path: str) -> bool:
        """Return True if input has at least one audio stream."""
        try:
            import subprocess, os as _os
            if _os.path.sep in ffmpeg or ("/" in ffmpeg) or ("\\" in ffmpeg):
                base_dir = _os.path.dirname(ffmpeg)
                exe = "ffprobe.exe" if _os.name == "nt" else "ffprobe"
                ffprobe = _os.path.join(base_dir, exe)
            else:
                ffprobe = "ffprobe"
            args = [
                ffprobe,
                "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                path,
            ]
            out = subprocess.check_output(args, stderr=subprocess.STDOUT, text=True).strip()
            return bool(out)
        except Exception:
            return False

    # Build filtergraph: scale and crop to 16:9
    try:
        z = max(1.0, min(float(zoom), 8.0))
    except Exception:
        z = 1.0
    
    # Scale height to target*zoom, then crop to target_w x target_h
    scaled_h = int(target_h * z)
    scale = f"scale=-2:{scaled_h}"
    crop = f"crop={target_w}:{target_h}:((in_w-{target_w})*{max(0.0, min(1.0, crop_x_pct))}):((in_h-{target_h})*{max(0.0, min(1.0, crop_y_pct))})"
    
    vf = f"{scale},{crop}"

    # Add input trimming if start/end times are specified
    input_args = ["-y"]
    if start_time > 0 or end_time > 0:
        if end_time > 0:
            duration = end_time - start_time
            input_args.extend(["-ss", str(start_time), "-t", str(duration), "-i", input_video])
        else:
            input_args.extend(["-ss", str(start_time), "-i", input_video])
    else:
        input_args.extend(["-i", input_video])
    
    cmd = [ffmpeg] + input_args
    
    # Determine render duration for correct fade-out timing
    if end_time and end_time > start_time:
        render_duration = max(0.0, float(end_time - start_time))
    else:
        render_duration = _probe_duration_seconds(input_video)

    has_audio = _probe_has_audio(input_video)

    if music_path and os.path.exists(music_path):
        # Audio filter with music
        delay_ms = max(0, int(music_offset_s * 1000))
        music_chain = f"[1:a]adelay={delay_ms}|{delay_ms}[am]"
        fo_start = max(0.0, (render_duration or 0.0) - float(fade_out_s or 0.0))

        post_fades = []
        if fade_in_s and float(fade_in_s) > 0:
            post_fades.append(f"afade=t=in:st=0:d={float(fade_in_s)}")
        if fade_out_s and float(fade_out_s) > 0:
            post_fades.append(f"afade=t=out:st={fo_start}:d={float(fade_out_s)}")
        post_chain = ",".join(post_fades) if post_fades else "anull"

        if has_audio:
            fc = (
                f"[0:v]{vf}[v];"
                f"{music_chain};"
                f"[0:a][am]amix=inputs=2:duration=first:dropout_transition=0[a0];"
                f"[a0]{post_chain}[a]"
            )
            cmd += ["-i", music_path, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        else:
            fc = (
                f"[0:v]{vf}[v];"
                f"{music_chain};"
                f"[am]{post_chain}[a]"
            )
            cmd += ["-i", music_path, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    else:
        # No external music
        if has_audio and ((fade_in_s and fade_in_s > 0) or (fade_out_s and fade_out_s > 0)):
            fo_start = max(0.0, (render_duration or 0.0) - float(fade_out_s or 0.0))
            parts = []
            if fade_in_s and float(fade_in_s) > 0:
                parts.append(f"afade=t=in:st=0:d={float(fade_in_s)}")
            if fade_out_s and float(fade_out_s) > 0:
                parts.append(f"afade=t=out:st={fo_start}:d={float(fade_out_s)}")
            post_chain = ",".join(parts) if parts else "anull"
            fc = f"[0:v]{vf}[v];[0:a]{post_chain}[a]"
            cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        else:
            cmd += ["-vf", vf]

    # Output settings
    cmd += [
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "22",
        "-movflags", "+faststart",
        output_video,
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1000:]}")


def extract_poster_frame(video_path: str, out_path: str, at_sec: float = 1.0) -> Optional[str]:
    """Grab a single JPEG frame from a video for use as a thumbnail/poster.

    Returns the output path on success, or None on failure.
    """
    try:
        ffmpeg = _ffmpeg_path()
        info = _probe_video_info(video_path)
        dur = float(info.get("duration") or 0.0)
        # Clamp the timestamp to inside the clip.
        ts = at_sec if (dur <= 0 or at_sec < dur) else max(0.0, dur / 2.0)
        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        cmd = [
            ffmpeg, "-y",
            "-ss", str(ts),
            "-i", video_path,
            "-frames:v", "1",
            "-q:v", "3",
            out_path,
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode != 0 or not os.path.exists(out_path):
            return None
        return out_path
    except Exception:
        return None


def is_already_vertical(video_path: str) -> bool:
    """True if the source video is already portrait (height > width), so the
    9:16 crop is effectively a centered pass-through."""
    info = _probe_video_info(video_path)
    w = int(info.get("width") or 0)
    h = int(info.get("height") or 0)
    return h > 0 and w > 0 and h > w


def render_vertical_with_music(
    input_video: str,
    output_video: str,
    crop_x_pct: float = 0.5,
    crop_y_pct: float = 0.5,
    music_path: Optional[str] = None,
    music_offset_s: float = 0.0,
    fade_in_s: float = 1.0,
    fade_out_s: float = 1.0,
    start_time: float = 0.0,
    end_time: float = 0.0,
    zoom: float = 1.0,
) -> None:
    """
    Render a vertical 9:16 video from a landscape source.
    Steps: scale to height 1920, crop 1080x1920 using provided offsets, optional music mix with fades.
    """
    ffmpeg = _ffmpeg_path()
    
    def _probe_duration_seconds(path: str) -> float:
        """Return media duration in seconds using ffprobe; 0.0 on failure."""
        try:
            import subprocess, json, os as _os
            # Resolve ffprobe in the same directory as ffmpeg when using a full path
            if _os.path.sep in ffmpeg or ("/" in ffmpeg) or ("\\" in ffmpeg):
                base_dir = _os.path.dirname(ffmpeg)
                exe = "ffprobe.exe" if _os.name == "nt" else "ffprobe"
                ffprobe = _os.path.join(base_dir, exe)
            else:
                ffprobe = "ffprobe"
            args = [
                ffprobe,
                "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "format=duration",
                "-of", "default=nk=1:nw=1",
                path,
            ]
            out = subprocess.check_output(args, stderr=subprocess.STDOUT, text=True).strip()
            return float(out)
        except Exception:
            return 0.0

    def _probe_has_audio(path: str) -> bool:
        """Return True if input has at least one audio stream."""
        try:
            import subprocess, os as _os
            if _os.path.sep in ffmpeg or ("/" in ffmpeg) or ("\\" in ffmpeg):
                base_dir = _os.path.dirname(ffmpeg)
                exe = "ffprobe.exe" if _os.name == "nt" else "ffprobe"
                ffprobe = _os.path.join(base_dir, exe)
            else:
                ffprobe = "ffprobe"
            args = [
                ffprobe,
                "-v", "error",
                "-select_streams", "a",
                "-show_entries", "stream=index",
                "-of", "csv=p=0",
                path,
            ]
            out = subprocess.check_output(args, stderr=subprocess.STDOUT, text=True).strip()
            return bool(out)
        except Exception:
            return False

    # Build filtergraph: scale and crop
    # Scale height to 1920, preserve aspect, width becomes sw
    # Compute crop x based on percentage of (sw - 1080)
    # We use FFmpeg expressions to compute crop dynamically
    # Clamp zoom to sensible range
    try:
        z = max(1.0, min(float(zoom), 8.0))
    except Exception:
        z = 1.0
    # Scale height to 1920*zoom to simulate zoom-in before cropping
    target_h = int(1920 * z)
    scale = f"scale=-2:{target_h}"
    crop = f"crop=1080:1920:((in_w-1080)*{max(0.0, min(1.0, crop_x_pct))}):((in_h-1920)*{max(0.0, min(1.0, crop_y_pct))})"

    vf = f"{scale},{crop}"

    # Add input trimming if start/end times are specified
    input_args = ["-y"]
    if start_time > 0 or end_time > 0:
        if end_time > 0:
            duration = end_time - start_time
            input_args.extend(["-ss", str(start_time), "-t", str(duration), "-i", input_video])
        else:
            input_args.extend(["-ss", str(start_time), "-i", input_video])
    else:
        input_args.extend(["-i", input_video])
    
    cmd = [ffmpeg] + input_args
    
    # Determine render duration for correct fade-out timing (post-trim duration)
    if end_time and end_time > start_time:
        render_duration = max(0.0, float(end_time - start_time))
    else:
        render_duration = _probe_duration_seconds(input_video)

    has_audio = _probe_has_audio(input_video)

    if music_path and os.path.exists(music_path):
        # Audio filter with music. If input has no audio, use only music track.
        delay_ms = max(0, int(music_offset_s * 1000))
        # Prepare delayed music track
        music_chain = f"[1:a]adelay={delay_ms}|{delay_ms}[am]"
        # Compute fade-out start relative to render duration
        fo_start = max(0.0, (render_duration or 0.0) - float(fade_out_s or 0.0))

        # Build filter_complex: mix original audio with delayed music, then apply fades to mix
        post_fades = []
        if fade_in_s and float(fade_in_s) > 0:
            post_fades.append(f"afade=t=in:st=0:d={float(fade_in_s)}")
        if fade_out_s and float(fade_out_s) > 0:
            post_fades.append(f"afade=t=out:st={fo_start}:d={float(fade_out_s)}")
        post_chain = ",".join(post_fades) if post_fades else "anull"

        if has_audio:
            fc = (
                f"[0:v]{vf}[v];"
                f"{music_chain};"
                f"[0:a][am]amix=inputs=2:duration=first:dropout_transition=0[a0];"
                f"[a0]{post_chain}[a]"
            )
            cmd += ["-i", music_path, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        else:
            # No input audio: use only delayed music, then apply fades
            fc = (
                f"[0:v]{vf}[v];"
                f"{music_chain};"
                f"[am]{post_chain}[a]"
            )
            cmd += ["-i", music_path, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
    else:
        # No external music
        if has_audio and ((fade_in_s and fade_in_s > 0) or (fade_out_s and fade_out_s > 0)):
            # Apply fades to input audio only if audio exists
            fo_start = max(0.0, (render_duration or 0.0) - float(fade_out_s or 0.0))
            parts = []
            if fade_in_s and float(fade_in_s) > 0:
                parts.append(f"afade=t=in:st=0:d={float(fade_in_s)}")
            if fade_out_s and float(fade_out_s) > 0:
                parts.append(f"afade=t=out:st={fo_start}:d={float(fade_out_s)}")
            post_chain = ",".join(parts) if parts else "anull"
            fc = f"[0:v]{vf}[v];[0:a]{post_chain}[a]"
            cmd += ["-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
        else:
            # No audio present or no fades needed: just video processing (no audio mapping)
            cmd += ["-vf", vf]

    # Output settings
    cmd += [
        "-c:v", "libx264", "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p",
        "-preset", "veryfast", "-crf", "22",
        "-movflags", "+faststart",
        output_video,
    ]

    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {proc.stderr[-1000:]}")


