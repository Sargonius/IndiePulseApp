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
    
    raise RuntimeError(
        "FFmpeg not found. Please install FFmpeg and add it to PATH, "
        "or place ffmpeg.exe in vendor/ffmpeg/ directory. "
        "Download from: https://ffmpeg.org/download.html"
    )


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

    if music_path and os.path.exists(music_path):
        # Audio filter for music: delay start, apply fade in/out, then mix with original
        # Convert delay to ms
        delay_ms = max(0, int(music_offset_s * 1000))
        # Build audio filters
        af_parts = []
        af_parts.append(f"[1:a]adelay={delay_ms}|{delay_ms}")
        if fade_in_s and fade_in_s > 0:
            af_parts[-1] += f",afade=t=in:st={music_offset_s}:d={fade_in_s}"
        # For fade-out we need estimated out start; approximate using video duration during mixing with shortest/duration=first
        # Use t'fade_out' near the end; as we do not know duration here, we can still apply fade out with start after offset
        if fade_out_s and fade_out_s > 0:
            # start fade-out at offset + 1s as minimal; user should adjust if needed in subsequent renders
            af_parts[-1] += f",afade=t=out:st={max(0.0, music_offset_s)}:d={fade_out_s}"
        af_parts[-1] += "[am]"

        a_filter = ",".join(af_parts)
        # Full filter_complex combines video and audio
        fc = f"[0:v]{vf}[v];{a_filter};[0:a][am]amix=inputs=2:duration=first:dropout_transition=0[a]"
        cmd += ["-i", music_path, "-filter_complex", fc, "-map", "[v]", "-map", "[a]"]
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


