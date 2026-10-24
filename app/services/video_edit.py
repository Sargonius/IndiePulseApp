"""The video editor behind every page that takes a video (Create Post, Create Video).

One ffmpeg pass: reframe to a format (as is / 16:9 / 9:16 / 1:1) with a movable
crop and zoom, trim, mix in a library track, burn in text overlays (the same PNGs
as the Shorts Factory, text_overlay.py). The result is a new file in media/; the
source stays, and a sidecar in render/edits/ keeps the source + settings so the
editor can reopen an edited clip with the same settings.

Renders run on a background thread, one at a time; the page polls the status.
Whether a file fits each platform is answered by fit_report(), with the same
numbers the publisher uses to compress (video_editor.compression_plan).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from typing import Optional

from loguru import logger

from app.services.text_overlay import clean_overlays, clean_trim, render_overlay_png

MEDIA_DIR = "media"
EDITS_DIR = os.path.join("render", "edits")

FORMATS = {
    "original": {"label": "Original", "size": None},
    "landscape": {"label": "16:9", "size": (1920, 1080)},
    "vertical": {"label": "9:16", "size": (1080, 1920)},
    "square": {"label": "1:1", "size": (1080, 1080)},
}
MAX_LONG_SIDE = 1920  # "as is" keeps the aspect but not a 4K frame
MAX_ZOOM = 4.0

# Platform labels for the fit report
LABELS = {
    "vk": "VK", "x": "X", "discord": "Discord", "fb_ru": "Facebook RU", "fb_en": "Facebook EN",
    "bluesky": "Bluesky", "reddit": "Reddit", "tg": "Telegram", "yt": "YouTube", "tiktok": "TikTok",
    "vk_video": "VK Video", "ig_reels": "Instagram",
}
# Longest video a platform takes through our API path, seconds.
DURATION_LIMITS = {
    "x": 140,        # media_category=tweet_video (no Premium)
    "bluesky": 180,  # video.bsky.app
}

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---- Paths ---------------------------------------------------------------------
class EditError(ValueError):
    """Bad input from the page (unknown file, foreign music path, ...)."""


def media_abs(rel: str) -> str:
    """Absolute path of a file in media/ given its rel path ('x.mp4' or 'media/x.mp4').
    Refuses anything outside media/."""
    rel = (rel or "").replace("\\", "/").strip()
    if rel.startswith("/media/"):
        rel = rel[len("/media/"):]
    elif rel.startswith("media/"):
        rel = rel[len("media/"):]
    if not rel or ".." in rel.split("/"):
        raise EditError("bad media path")
    base = os.path.realpath(MEDIA_DIR)
    path = os.path.realpath(os.path.join(base, rel))
    try:
        inside = os.path.commonpath([os.path.normcase(path), os.path.normcase(base)]) == os.path.normcase(base)
    except ValueError:  # another drive
        inside = False
    if not inside:
        raise EditError("bad media path")
    if not os.path.isfile(path):
        raise EditError("file not found")
    return path


def media_rel(path: str) -> str:
    return os.path.relpath(os.path.realpath(path), os.path.realpath(MEDIA_DIR)).replace("\\", "/")


def _ffmpeg() -> str:
    from app.services.video_editor import _ffmpeg_path
    ff = _ffmpeg_path()
    return os.path.abspath(ff) if (os.sep in ff or "/" in ff) else ff


def _ffprobe() -> str:
    ff = _ffmpeg()
    if os.sep in ff or "/" in ff:
        return os.path.join(os.path.dirname(ff), "ffprobe.exe" if os.name == "nt" else "ffprobe")
    return "ffprobe"


def probe(path: str) -> dict:
    """{duration, width, height (as displayed, rotation applied), has_audio, fps, size_mb}."""
    info = {"duration": 0.0, "width": 0, "height": 0, "has_audio": False, "fps": 0.0,
            "size_mb": round(os.path.getsize(path) / (1024 * 1024), 2) if os.path.exists(path) else 0.0}
    try:
        out = subprocess.run(
            [_ffprobe(), "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height,avg_frame_rate:stream_tags=rotate:stream_side_data=rotation",
             "-of", "json", path],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30, creationflags=_NO_WINDOW,
        )
        data = json.loads(out.stdout or "{}")
    except Exception as exc:
        logger.warning(f"probe failed for {path}: {exc}")
        return info
    try:
        info["duration"] = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        pass
    for st in data.get("streams") or []:
        kind = st.get("codec_type")
        if kind == "audio":
            info["has_audio"] = True
        elif kind == "video" and not info["width"]:
            w, h = int(st.get("width") or 0), int(st.get("height") or 0)
            rot = 0
            try:
                rot = int(float((st.get("tags") or {}).get("rotate") or 0))
            except (TypeError, ValueError):
                pass
            for sd in st.get("side_data_list") or []:
                if "rotation" in sd:
                    try:
                        rot = int(float(sd["rotation"]))
                    except (TypeError, ValueError):
                        pass
            if abs(rot) % 180 == 90:  # phone footage: ffmpeg autorotates while encoding
                w, h = h, w
            info["width"], info["height"] = w, h
            try:
                num, den = (st.get("avg_frame_rate") or "0/1").split("/")
                info["fps"] = round(float(num) / float(den), 2) if float(den) else 0.0
            except (ValueError, ZeroDivisionError):
                pass
    info["duration"] = round(info["duration"], 3)
    return info


# ---- Settings from the page -------------------------------------------------------
def _num(v, default: float, lo: float, hi: float) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    if x != x:  # NaN
        return default
    return max(lo, min(hi, x))


def output_size(fmt: str, src_w: int, src_h: int) -> tuple[int, int]:
    size = (FORMATS.get(fmt) or {}).get("size")
    if size:
        return size
    if not src_w or not src_h:
        return (1920, 1080)
    k = min(1.0, MAX_LONG_SIDE / max(src_w, src_h))
    return (max(2, int(round(src_w * k / 2)) * 2), max(2, int(round(src_h * k / 2)) * 2))


def clean_spec(raw: Optional[dict], duration: Optional[float] = None) -> dict:
    """Validated editor settings. Times are source seconds for the trim and
    seconds of the trimmed clip for the overlays."""
    raw = raw if isinstance(raw, dict) else {}
    fmt = raw.get("format") if raw.get("format") in FORMATS else "original"
    ts, te = clean_trim(raw.get("trim_start"), raw.get("trim_end"))
    if duration and duration > 0:
        if ts is not None and ts >= duration - 0.5:
            ts = None
        if te is not None and te >= duration - 0.05:
            te = None
    clip = (te if te is not None else (duration or 0)) - (ts or 0)
    music = raw.get("music") if isinstance(raw.get("music"), dict) else {}
    mpath = str(music.get("path") or "").strip()
    music_spec = None
    if mpath:
        music_spec = {
            "path": mpath,
            "start": round(_num(music.get("start"), 0.0, 0.0, 3600.0), 2),   # seconds into the track
            "volume": round(_num(music.get("volume"), 0.8, 0.0, 2.0), 2),
            "fade_in": round(_num(music.get("fade_in"), 0.5, 0.0, 10.0), 2),
            "fade_out": round(_num(music.get("fade_out"), 1.5, 0.0, 10.0), 2),
        }
    return {
        "format": fmt,
        "crop_x": round(_num(raw.get("crop_x"), 0.5, 0.0, 1.0), 3),
        "crop_y": round(_num(raw.get("crop_y"), 0.5, 0.0, 1.0), 3),
        "zoom": round(_num(raw.get("zoom"), 1.0, 1.0, MAX_ZOOM), 3),
        "trim_start": ts,
        "trim_end": te,
        "source_volume": round(_num(raw.get("source_volume"), 1.0, 0.0, 2.0), 2),
        "music": music_spec,
        "overlays": clean_overlays(raw.get("overlays"), clip if clip > 0 else None),
    }


def short_spec(raw: Optional[dict], duration: Optional[float] = None) -> dict:
    """A Shorts Factory clip's settings: the editor's spec, always 9:16."""
    raw = dict(raw) if isinstance(raw, dict) else {}
    raw["format"] = "vertical"
    return clean_spec(raw, duration)


def spec_hash(spec: dict) -> str:
    return hashlib.sha1(json.dumps(spec, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:8]


def clip_duration(spec: dict, src_duration: float) -> float:
    end = spec.get("trim_end") or src_duration
    return max(0.0, float(end) - float(spec.get("trim_start") or 0))


def is_noop(spec: dict, info: dict) -> bool:
    """Nothing to do: the file as it is would come out (no trim/music/text/reframe)."""
    return (spec["format"] == "original" and spec["zoom"] == 1.0 and not spec["trim_start"]
            and not spec["trim_end"] and not spec["music"] and not spec["overlays"]
            and spec["source_volume"] == 1.0
            and max(info.get("width") or 0, info.get("height") or 0) <= MAX_LONG_SIDE)


# ---- ffmpeg command ---------------------------------------------------------------
def build_command(ffmpeg: str, src: str, out: str, spec: dict, info: dict,
                  overlay_pngs: Optional[list] = None, music_path: Optional[str] = None,
                  music_duration: float = 0.0) -> list[str]:
    """The whole edit as one ffmpeg call. `overlay_pngs` are (png, start, end) at the
    output frame size; `music_path` is already checked against the library."""
    W, H = output_size(spec["format"], info.get("width") or 0, info.get("height") or 0)
    ts = float(spec.get("trim_start") or 0)
    clip = clip_duration(spec, float(info.get("duration") or 0)) or float(info.get("duration") or 0)
    cmd = [ffmpeg, "-nostdin", "-hide_banner", "-y", "-progress", "pipe:1", "-nostats"]
    if ts > 0:
        cmd += ["-ss", f"{ts:.3f}"]
    if spec.get("trim_end"):
        cmd += ["-t", f"{clip:.3f}"]
    cmd += ["-i", src]
    pngs = [(p, float(s), float(e)) for (p, s, e) in (overlay_pngs or []) if e > s]
    for png, _s, _e in pngs:
        cmd += ["-i", png]
    music = spec.get("music") if music_path else None
    music_idx = 1 + len(pngs)
    if music:
        if music["start"] > 0:
            cmd += ["-ss", f"{music['start']:.3f}"]
        if music_duration and music_duration - music["start"] < clip:
            cmd += ["-stream_loop", "-1"]  # a short track loops under a long clip
        cmd += ["-i", music_path]

    # Video: cover-scale (with zoom) to the frame, crop at the chosen spot
    z = float(spec.get("zoom") or 1.0)
    sw, sh = int(round(W * z / 2)) * 2, int(round(H * z / 2)) * 2
    cx, cy = float(spec.get("crop_x", 0.5)), float(spec.get("crop_y", 0.5))
    chain = [f"[0:v]scale={sw}:{sh}:force_original_aspect_ratio=increase,"
             f"crop={W}:{H}:(iw-ow)*{cx:.4f}:(ih-oh)*{cy:.4f},setsar=1[v0]"]
    last = "[v0]"
    for i, (_png, s, e) in enumerate(pngs, start=1):
        nxt = f"[v{i}]"
        chain.append(f"{last}[{i}:v]overlay=0:0:enable='between(t,{s:.3f},{e:.3f})'{nxt}")
        last = nxt
    chain.append(f"{last}format=yuv420p[v]")

    # Audio: the game's own sound (volume 0 = off) + the track with its fades
    src_vol = float(spec.get("source_volume", 1.0))
    use_src = bool(info.get("has_audio")) and src_vol > 0
    if use_src:
        chain.append(f"[0:a]volume={src_vol:.2f}[as]")
    if music:
        fades = [f"atrim=0:{clip:.3f}", "asetpts=PTS-STARTPTS", f"volume={music['volume']:.2f}"]
        if music["fade_in"] > 0:
            fades.append(f"afade=t=in:st=0:d={music['fade_in']:.2f}")
        if music["fade_out"] > 0:
            fades.append(f"afade=t=out:st={max(0.0, clip - music['fade_out']):.3f}:d={music['fade_out']:.2f}")
        chain.append(f"[{music_idx}:a]{','.join(fades)}[am]")
    if use_src and music:
        chain.append(f"[as][am]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
                     f"atrim=0:{clip:.3f}[a]")
    elif use_src:
        chain.append("[as]anull[a]")
    elif music:
        chain.append("[am]anull[a]")
    has_out_audio = use_src or bool(music)

    cmd += ["-filter_complex", ";".join(chain), "-map", "[v]"]
    cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "160k", "-ar", "48000"] if has_out_audio else ["-an"]
    if clip > 0:
        cmd += ["-t", f"{clip:.3f}"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-maxrate", "12M", "-bufsize", "24M",
            "-profile:v", "high", "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-f", "mp4", out]
    return cmd


# ---- Output files -------------------------------------------------------------------
def _safe_stem(name: str) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", os.path.splitext(os.path.basename(name))[0]).strip("._")
    return (stem or "clip")[:60]


def output_rel(src_rel: str, spec: dict) -> str:
    sig = json.dumps([src_rel, spec], ensure_ascii=False, sort_keys=True)
    h = hashlib.sha1(sig.encode("utf-8")).hexdigest()[:8]
    code = {"original": "edit", "landscape": "16x9", "vertical": "9x16", "square": "1x1"}[spec["format"]]
    return f"{_safe_stem(src_rel)}_{code}_{h}.mp4"


def _sidecar(out_rel: str) -> str:
    return os.path.join(EDITS_DIR, os.path.splitext(os.path.basename(out_rel))[0] + ".json")


def load_edit(out_rel: str) -> Optional[dict]:
    """{src, spec, out, ...} of an edited file, or None for a file that wasn't made here."""
    try:
        with open(_sidecar(out_rel), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) and data.get("src") else None
    except (OSError, ValueError):
        return None


def write_sidecar(out_rel: str, src_rel: str, spec: dict, res: Optional[dict] = None) -> None:
    """Remember what `out_rel` was made from, so the editor reopens it with its source and
    settings. Also used for Shorts Factory renders, which run outside this module's jobs."""
    res = res or _result(media_abs(out_rel))
    os.makedirs(EDITS_DIR, exist_ok=True)
    with open(_sidecar(out_rel), "w", encoding="utf-8") as fh:
        json.dump({"src": src_rel, "spec": spec, "out": out_rel, "created": int(time.time()),
                   **{k: res[k] for k in ("size_mb", "duration", "width", "height")}},
                  fh, ensure_ascii=False, indent=1)


def _result(out_abs: str, extra: Optional[dict] = None) -> dict:
    info = probe(out_abs)
    rel = media_rel(out_abs)
    res = {"out_rel": rel, "url": f"/media/{rel}", "size_mb": info["size_mb"],
           "duration": info["duration"], "width": info["width"], "height": info["height"]}
    res.update(extra or {})
    return res


# ---- Background renders ----------------------------------------------------------------
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_one_at_a_time = threading.Lock()  # ffmpeg already uses every core
MAX_KEPT_JOBS = 50


def _public(job: dict) -> dict:
    keys = ("id", "status", "progress", "error", "src", "out_rel", "url", "size_mb", "duration",
            "width", "height", "spec")
    return {k: job.get(k) for k in keys if k in job}


def get_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return _public(job) if job else None


def cancel_job(job_id: str) -> bool:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job["status"] not in ("queued", "running"):
            return False
        job["cancel"] = True
        proc = job.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
    return True


def start_render(src_rel: str, raw_spec: Optional[dict], music_ok=None) -> dict:
    """Validate and queue one render → the job's public dict. Raises EditError on bad
    input. `music_ok(path) -> bool` decides whether the track may be read."""
    src_abs = media_abs(src_rel)
    src_rel = media_rel(src_abs)
    info = probe(src_abs)
    if not info["width"] or not info["height"]:
        raise EditError("not a video (ffprobe found no picture)")
    spec = clean_spec(raw_spec, info["duration"])
    if is_noop(spec, info):  # nothing changed: keep the file itself
        return {"id": None, "status": "done", "progress": 1.0, "noop": True, "src": src_rel, "spec": spec,
                "out_rel": src_rel, "url": f"/media/{src_rel}", "size_mb": info["size_mb"],
                "duration": info["duration"], "width": info["width"], "height": info["height"]}
    if spec["music"]:
        mp = spec["music"]["path"]
        if not (music_ok(mp) if music_ok else False):
            raise EditError("music track is not in the music folders")
    out_rel = output_rel(src_rel, spec)
    out_abs = os.path.join(os.path.realpath(MEDIA_DIR), out_rel)
    job = {"id": uuid.uuid4().hex[:12], "status": "queued", "progress": 0.0, "src": src_rel,
           "spec": spec, "out_rel": out_rel, "info": info, "created": time.time()}
    if os.path.isfile(out_abs) and load_edit(out_rel):
        job.update(status="done", progress=1.0, **_result(out_abs))  # same settings: already rendered
    with _jobs_lock:
        _jobs[job["id"]] = job
        for old in sorted(_jobs.values(), key=lambda j: j["created"])[:-MAX_KEPT_JOBS]:
            if old["status"] not in ("queued", "running"):
                _jobs.pop(old["id"], None)
    if job["status"] == "queued":
        threading.Thread(target=_run, args=(job,), name=f"video-edit-{job['id']}", daemon=True).start()
    return _public(job)


def _set(job: dict, **kw) -> None:
    with _jobs_lock:
        job.update(kw)


def _run(job: dict) -> None:
    with _one_at_a_time:
        if job.get("cancel"):
            _set(job, status="cancelled")
            return
        _set(job, status="running")
        try:
            _render(job)
        except Exception as exc:  # report, never kill the server thread silently
            logger.error(f"video edit {job['id']} failed: {exc}")
            _set(job, status="cancelled" if job.get("cancel") else "error", error=str(exc)[-1500:])


class RenderCancelled(RuntimeError):
    pass


def render_file(src_abs: str, out_abs: str, spec: dict, info: Optional[dict] = None,
                on_progress=None, on_proc=None, cancelled=None) -> None:
    """Render `spec` (already cleaned, music already checked) from src to out in one
    ffmpeg pass. Writes `<out>.part` and renames it, so a failed render never leaves
    a half file under the real name. Used by the editor's background renders and by
    the Shorts Factory's render_short job (which runs in the worker process)."""
    info = info or probe(src_abs)
    part = out_abs + ".part"
    W, H = output_size(spec["format"], info["width"], info["height"])
    clip = clip_duration(spec, info["duration"]) or info["duration"]
    tmp = tempfile.mkdtemp(prefix="ip_edit_")
    log_path = os.path.join(tmp, "ffmpeg.log")
    try:
        pngs = []
        for i, o in enumerate(spec["overlays"]):
            path = os.path.join(tmp, f"ovl_{i}.png")
            render_overlay_png(o["text"], o["pos"], o["size"], out_path=path, frame=(W, H))
            pngs.append((path, o["start"], o["end"]))
        music_path = spec["music"]["path"] if spec["music"] else None
        music_len = 0.0
        if music_path:
            from app.services.music_library import duration as track_duration
            music_len = track_duration(music_path)
        cmd = build_command(_ffmpeg(), src_abs, part, spec, info, pngs, music_path, music_len)
        with open(log_path, "w", encoding="utf-8", errors="replace") as log:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=log,
                                    text=True, creationflags=_NO_WINDOW)
            if on_proc:
                on_proc(proc)
            for line in proc.stdout:  # -progress: key=value lines
                if on_progress and line.startswith(("out_time_us=", "out_time_ms=")) and clip > 0:
                    try:
                        done = int(line.split("=", 1)[1]) / 1_000_000
                    except ValueError:
                        continue
                    on_progress(round(max(0.0, min(0.99, done / clip)), 3))
            code = proc.wait()
        if on_proc:
            on_proc(None)
        if cancelled and cancelled():
            raise RenderCancelled("cancelled")
        if code != 0 or not os.path.isfile(part):
            with open(log_path, encoding="utf-8", errors="replace") as fh:
                tail = fh.read()[-1200:]
            raise RuntimeError(f"ffmpeg exit {code}: {tail}")
        os.replace(part, out_abs)
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _render(job: dict) -> None:
    spec, info = job["spec"], job["info"]
    out_abs = os.path.join(os.path.realpath(MEDIA_DIR), job["out_rel"])
    W, H = output_size(spec["format"], info["width"], info["height"])
    os.makedirs(EDITS_DIR, exist_ok=True)
    try:
        render_file(media_abs(job["src"]), out_abs, spec, info,
                    on_progress=lambda p: _set(job, progress=p),
                    on_proc=lambda proc: _set(job, proc=proc),
                    cancelled=lambda: bool(job.get("cancel")))
    except RenderCancelled:
        _set(job, status="cancelled")
        return
    res = _result(out_abs)
    write_sidecar(job["out_rel"], job["src"], spec, res)
    _set(job, status="done", progress=1.0, **res)
    logger.info(f"video edit {job['id']}: {job['src']} -> {job['out_rel']} "
                f"({W}x{H}, {res['duration']:.1f} s, {res['size_mb']:.1f} MB)")


# ---- Will it fit the platforms? -------------------------------------------------------
def _mmss(sec: float) -> str:
    sec = int(round(sec))
    return f"{sec // 60}:{sec % 60:02d}"


def fit_report(path: str, platforms: list[str], cfg: Optional[dict] = None) -> dict:
    """Per selected platform: does this video go as is, get compressed on publish, or
    not fit at all. Mirrors ensure_video_size_limit / compress_video_to_size."""
    from app.services.settings import load_settings
    from app.services.video_editor import (MIN_VIDEO_KBPS, compression_plan, max_fit_duration,
                                           platform_video_limit_mb)
    cfg = cfg or load_settings()
    info = probe(path)
    size, dur = info["size_mb"], info["duration"]
    rows = []
    for p in platforms:
        if p not in LABELS:
            continue
        limit = platform_video_limit_mb(p, cfg)
        target = limit * 0.95
        row = {"platform": p, "label": LABELS[p], "limit_mb": limit, "level": "ok"}
        if size <= target:
            row["text"] = f"{size:.1f} MB — fits (limit {limit:g} MB)"
        else:
            plan = compression_plan(dur, info["width"], info["height"], target)
            kbps, side = plan["attempts"][0]
            if dur <= 0 or kbps < MIN_VIDEO_KBPS:
                row["level"] = "error"
                row["text"] = (f"{size:.1f} MB won’t fit in {limit:g} MB even compressed — video {_mmss(dur)}, "
                               f"max ≈ {_mmss(max_fit_duration(target))}. Trim it or untick the network.")
            else:
                res = f"{side}p" if side else f"{min(info['width'], info['height'])}p"
                row["level"] = "warn" if kbps < 1500 else "info"
                row["text"] = (f"{size:.1f} MB > {limit:g} MB — compressed to ≈{target:.1f} MB on publish "
                               f"({res}, ~{kbps / 1000:.1f} Mbps)")
        max_s = DURATION_LIMITS.get(p)
        if max_s and dur > max_s:
            row["level"] = "error"
            row["text"] = f"Video {_mmss(dur)} — {LABELS[p]} takes {_mmss(max_s)} max. " + row["text"]
        rows.append(row)
    return {"size_mb": size, "duration": dur, "width": info["width"], "height": info["height"], "rows": rows}
