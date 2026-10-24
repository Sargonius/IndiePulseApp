"""Music for the video editors: tracks from the folders set in Settings
(`music_dir`, one folder per line), scanned with their subfolders.

Durations come from ffprobe and are cached by (path, mtime, size), so opening
an editor doesn't decode every track again.
"""
from __future__ import annotations

import os
import re
import subprocess
from typing import Optional

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac", ".wma", ".opus"}
MAX_DEPTH = 3        # subfolder levels under each music folder
MAX_TRACKS = 2000

_durations: dict[tuple, float] = {}


def folders(cfg: Optional[dict] = None) -> list[str]:
    """Music folders from the settings: one per line (or `;`-separated)."""
    if cfg is None:
        from app.services.settings import load_settings
        cfg = load_settings()
    out: list[str] = []
    for part in re.split(r"[\r\n;]+", str(cfg.get("music_dir") or "")):
        p = part.strip().strip('"').strip()
        if p and p not in out:
            out.append(p)
    return out


def _ffprobe() -> Optional[str]:
    try:
        from app.services.video_editor import _ffmpeg_path
        ff = _ffmpeg_path()
    except Exception:
        return None
    if os.sep in ff or "/" in ff:
        return os.path.join(os.path.dirname(ff), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    return "ffprobe"


def duration(path: str) -> float:
    """Track length in seconds (0.0 when it can't be read)."""
    try:
        st = os.stat(path)
    except OSError:
        return 0.0
    key = (os.path.abspath(path), st.st_mtime, st.st_size)
    if key in _durations:
        return _durations[key]
    secs = 0.0
    probe = _ffprobe()
    if probe:
        try:
            out = subprocess.run(
                [probe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nk=1:nw=1", path],
                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20,
                creationflags=0x08000000 if os.name == "nt" else 0,  # CREATE_NO_WINDOW
            )
            secs = float((out.stdout or "0").strip() or 0)
        except Exception:
            secs = 0.0
    _durations[key] = secs
    return secs


def list_tracks(cfg: Optional[dict] = None) -> list[dict]:
    """[{name, path, folder, duration_sec, duration_ms}] sorted by folder, then name.
    `folder` is the music folder's name plus the subfolder, for grouping in a select."""
    tracks: list[dict] = []
    for base in folders(cfg):
        if not os.path.isdir(base):
            continue
        base_label = os.path.basename(os.path.normpath(base)) or base
        base_depth = os.path.normpath(base).count(os.sep)
        for root, dirs, files in os.walk(base):
            dirs.sort()
            if os.path.normpath(root).count(os.sep) - base_depth >= MAX_DEPTH:
                dirs[:] = []
            rel = os.path.relpath(root, base)
            label = base_label if rel == "." else f"{base_label} / {rel.replace(os.sep, ' / ')}"
            for name in sorted(files, key=str.lower):
                if os.path.splitext(name)[1].lower() not in AUDIO_EXTS:
                    continue
                path = os.path.join(root, name)
                secs = duration(path)
                tracks.append({"name": name, "path": path, "folder": label,
                               "duration_sec": round(secs, 2), "duration_ms": int(secs * 1000)})
                if len(tracks) >= MAX_TRACKS:
                    return tracks
    return tracks


_peaks: dict[tuple, list] = {}
PEAK_RATE = 2000  # Hz of the mono PCM the peaks are taken from


def peaks(path: str, buckets: int = 600) -> list[float]:
    """Loudness outline of a track for the editor's waveform: `buckets` peaks in 0..1."""
    st = os.stat(path)
    key = (os.path.abspath(path), st.st_mtime, st.st_size, buckets)
    if key in _peaks:
        return _peaks[key]
    import array
    from app.services.video_editor import _ffmpeg_path
    proc = subprocess.run(
        [_ffmpeg_path(), "-v", "error", "-nostdin", "-i", path, "-ac", "1", "-ar", str(PEAK_RATE),
         "-f", "s16le", "-"],
        stdin=subprocess.DEVNULL, capture_output=True, timeout=60,
        creationflags=0x08000000 if os.name == "nt" else 0,
    )
    pcm = array.array("h")
    pcm.frombytes(proc.stdout[: len(proc.stdout) // 2 * 2])
    if not pcm:
        return []
    step = max(1, len(pcm) // buckets)
    out = [max(abs(min(pcm[i:i + step])), abs(max(pcm[i:i + step]))) for i in range(0, len(pcm), step)]
    top = max(out) or 1
    out = [round(v / top, 3) for v in out]
    if len(_peaks) > 64:
        _peaks.clear()
    _peaks[key] = out
    return out


def _inside(path: str, folder: str) -> bool:
    try:
        real, base = os.path.realpath(path), os.path.realpath(folder)
        return os.path.commonpath([os.path.normcase(real), os.path.normcase(base)]) == os.path.normcase(base)
    except ValueError:  # different drives on Windows
        return False


def is_allowed(path: str, cfg: Optional[dict] = None, extra: Optional[list[str]] = None) -> bool:
    """True for an audio file inside a music folder, or one of `extra` (the music a
    project or a clip preset points at). Everything the browser may play or the
    renderer may read goes through this."""
    if not path or not os.path.isfile(path):
        return False
    if os.path.splitext(path)[1].lower() not in AUDIO_EXTS:
        return False
    if any(_inside(path, f) for f in folders(cfg) if os.path.isdir(f)):
        return True
    real = os.path.normcase(os.path.realpath(path))
    return any(e and os.path.normcase(os.path.realpath(e)) == real for e in (extra or []))


def preset_music_paths(session) -> list[str]:
    """music_path of every project and clip preset (they may sit outside the folders)."""
    from sqlmodel import select
    from app.models.models import Preset, Project
    out: list[str] = []
    for model in (Project, Preset):
        try:
            out += [p for p in session.exec(select(model.music_path)).all() if p]
        except Exception:
            pass
    return out
