"""Text overlays: one transparent full-frame PNG per line of on-screen text,
composited by ffmpeg at render time (video_editor.render_vertical_with_music for
the Shorts Factory, video_edit.py for the general editor). The same code renders
the browser preview, so what the editor shows is exactly what gets burned in.

Style follows the guide (#lang): big bold font, white fill, black stroke,
centered, max 2 lines, always inside the safe zone. On a 9:16 frame that means
not in the bottom 20% or the top 10% (the Shorts/TikTok UI); other frames keep a
thin margin. The default frame is the Shorts one, 1080×1920: sizes are given
for it and scale with the short side of any other frame.
"""
from __future__ import annotations

import io
import os
import re
from functools import lru_cache
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

FRAME_W, FRAME_H = 1080, 1920
MAX_TEXT_W = 900                     # wrap width, px
SAFE_TOP = int(FRAME_H * 0.10)       # search bar / top chrome
SAFE_BOTTOM = int(FRAME_H * 0.80)    # caption + buttons below this
SAFE_PAD = 12

# Vertical center of the text block, as a share of the frame height.
POSITIONS = {"top": 0.16, "upper": 0.30, "middle": 0.48, "lower": 0.66}
SIZES = {"s": 64, "m": 84, "l": 108}  # starting font size, px
MIN_FONT = 52                         # shrink floor before wrapping to 3 lines
SHRINK_STEP = 6
MAX_LINES = 2
HARD_MAX_LINES = 3

MAX_OVERLAYS = 6
MAX_TEXT_LEN = 120

_FONTS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "fonts")
_WIN_FONTS = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
JP_FONTS = ["YuGothB.ttc", "meiryob.ttc", "BIZ-UDGothicB.ttc", "NotoSansJP-VF.ttf"]
LATIN_FONTS = ["arialbd.ttf", "segoeuib.ttf", "impact.ttf"]

# Kana, CJK ideographs, CJK punctuation, full-width forms: no spaces between words.
_CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")
_KANA_CJK_LETTER_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_CYR_RE = re.compile(r"[\u0400-\u04ff]")
_LAT_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
# Tokens for wrapping: one CJK char, a run of non-space non-CJK chars, or spaces.
_TOKEN_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
                       r"|[^\s\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]+|\s+")
# Japanese punctuation that must not start a line (kinsoku).
_NO_LINE_START = set("、。，．・：；？！ー）」』】〕〉》…‥ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")
# ...and opening brackets that must not end one: they move down with what they open.
_NO_LINE_END = set("（「『【〔〈《([{“‘")


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def detect_script(text: str) -> Optional[str]:
    """'ja' | 'cyr' | 'lat' | None (no letters). Any kana/kanji → 'ja';
    otherwise the majority of Cyrillic vs Latin letters wins."""
    t = text or ""
    if _KANA_CJK_LETTER_RE.search(t):
        return "ja"
    cyr, lat = len(_CYR_RE.findall(t)), len(_LAT_RE.findall(t))
    if not cyr and not lat:
        return None
    return "cyr" if cyr >= lat else "lat"


# ---- Fonts -------------------------------------------------------------------
def _candidates(cjk: bool) -> list[str]:
    out = []
    if os.path.isdir(_FONTS_DIR):  # bundled fonts win over system ones
        for name in sorted(os.listdir(_FONTS_DIR)):
            if name.lower().endswith((".ttf", ".otf", ".ttc")):
                out.append(os.path.join(_FONTS_DIR, name))
    for name in (JP_FONTS if cjk else LATIN_FONTS):
        out.append(os.path.join(_WIN_FONTS, name))
    return [p for p in out if os.path.isfile(p)]


@lru_cache(maxsize=64)
def _load_font(path: str, size: int) -> ImageFont.FreeTypeFont:
    font = ImageFont.truetype(path, size, index=0)
    if path.lower().endswith("-vf.ttf"):  # variable font: default instance is thin
        try:
            font.set_variation_by_name("Bold")
        except Exception:
            pass
    return font


def _glyph_bitmap(path: str, ch: str) -> bytes:
    img = Image.new("L", (64, 64), 0)
    ImageDraw.Draw(img).text((8, 8), ch, font=_load_font(path, 40), fill=255)
    return img.tobytes()


@lru_cache(maxsize=16)
def _notdef_bitmap(path: str) -> bytes:
    return _glyph_bitmap(path, "\U0010fffd")  # a code point no font has → .notdef box


@lru_cache(maxsize=4096)
def _has_glyph(path: str, ch: str) -> bool:
    return _glyph_bitmap(path, ch) != _notdef_bitmap(path)


def font_covers(path: str, text: str) -> bool:
    return all(_has_glyph(path, ch) for ch in set(text or "") if not ch.isspace())


def find_font(text: str) -> str:
    """Path of a bold font for `text`: Japanese-capable if it has kana/CJK,
    otherwise Latin+Cyrillic. The first candidate that has every glyph wins;
    failing that, the first candidate of the right kind."""
    cands = _candidates(has_cjk(text))
    if not cands:
        kind = "Japanese" if has_cjk(text) else "Latin/Cyrillic"
        raise RuntimeError(f"No bold font for {kind} text: put a .ttf in app/static/fonts/")
    for path in cands:
        try:
            if font_covers(path, text):
                return path
        except Exception:
            continue
    return cands[0]


# ---- Frame geometry ------------------------------------------------------------
FRAME = (FRAME_W, FRAME_H)


def _frame(frame) -> tuple[int, int]:
    try:
        w, h = int(frame[0]), int(frame[1])
    except Exception:
        return FRAME
    return (w, h) if 16 <= w <= 4096 and 16 <= h <= 4096 else FRAME


def frame_metrics(frame=FRAME) -> dict:
    """Scale, wrap width and safe zone of a frame. At 1080×1920 these are exactly
    the module constants, so the Shorts output doesn't change."""
    w, h = _frame(frame)
    scale = min(w, h) / 1080
    portrait = h > w * 1.3  # 9:16-like: the platform UI covers the top and bottom
    return {
        "w": w, "h": h, "scale": scale,
        # landscape text may run wider and still read as one block
        "max_w": round(w * MAX_TEXT_W / FRAME_W) if w <= h else round(w * 0.70),
        "safe_top": int(h * 0.10) if portrait else int(h * 0.06),
        "safe_bottom": int(h * 0.80) if portrait else int(h * 0.94),
        "pad": round(SAFE_PAD * scale),
    }


# ---- Layout ------------------------------------------------------------------
def _text_w(font, s: str, stroke: int) -> float:
    if not s:
        return 0.0
    l, _t, r, _b = font.getbbox(s, stroke_width=stroke)
    return float(r - l)


def wrap_text(text: str, font, max_w: int = MAX_TEXT_W, stroke: int = 0) -> list[str]:
    """Greedy word wrap. Spaces separate words; CJK wraps per character (with
    basic kinsoku); a single word wider than the line is broken by character.
    Explicit newlines are kept."""
    lines: list[str] = []
    for para in (text or "").split("\n"):
        cur = ""
        for tok in _TOKEN_RE.findall(para.strip()):
            if tok.isspace():
                if cur:
                    cur += " "
                continue
            cand = cur + tok
            if _text_w(font, cand, stroke) <= max_w:
                cur = cand
                continue
            if tok in _NO_LINE_START and cur.strip():  # keep 。、 on the previous line
                cur = cand
                continue
            carry = ""
            if cur.strip():
                head = cur.rstrip()
                if len(head) > 1 and head[-1] in _NO_LINE_END:  # 「（ go down with what they open
                    head, carry = head[:-1].rstrip(), head[-1]
                lines.append(head)
            cur = carry
            if _text_w(font, cur + tok, stroke) <= max_w:
                cur += tok
                continue
            for ch in tok:  # a single word wider than the line: break by character
                if cur and _text_w(font, cur + ch, stroke) > max_w:
                    lines.append(cur)
                    cur = ch
                else:
                    cur += ch
        if cur.strip():
            lines.append(cur.rstrip())
    return lines or []


def _stroke_for(size: int) -> int:
    return max(3, round(size / 11))


def layout_text(text: str, size: str = "m", font_path: Optional[str] = None,
                frame=FRAME) -> dict:
    """Pick the font size and line breaks for one overlay.

    Starts at SIZES[size] and shrinks step by step until the text fits in 2
    lines; below MIN_FONT it wraps to 3 lines (and truncates with «…» beyond).
    Returns {lines, font_size, font_path, fits (≤2 lines), shrunk, truncated}.
    """
    text = (text or "").strip()
    path = font_path or find_font(text)
    fm = frame_metrics(frame)
    max_w, k = fm["max_w"], fm["scale"]
    start = round(SIZES.get(size, SIZES["m"]) * k)
    min_font, step = round(MIN_FONT * k), max(2, round(SHRINK_STEP * k))
    fs = start
    while True:
        font = _load_font(path, fs)
        lines = wrap_text(text, font, max_w, _stroke_for(fs))
        if len(lines) <= MAX_LINES or fs - step < min_font:
            break
        fs -= step
    truncated = False
    if len(lines) > HARD_MAX_LINES:
        lines = lines[:HARD_MAX_LINES]
        font = _load_font(path, fs)
        last = lines[-1]
        while last and _text_w(font, last + "…", _stroke_for(fs)) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
        truncated = True
    return {"lines": lines, "font_size": fs, "font_path": path,
            "fits": len(lines) <= MAX_LINES and not truncated,
            "shrunk": fs < start, "truncated": truncated}


def _line_height(font) -> int:
    ascent, descent = font.getmetrics()
    return int((ascent + descent) * 1.08)


def block_box(text: str, pos: str = "middle", size: str = "m", frame=FRAME) -> dict:
    """Where the text block lands: {top, bottom, shifted, layout}. The block is
    centered on POSITIONS[pos] and pushed back inside the safe zone if needed."""
    fm = frame_metrics(frame)
    lay = layout_text(text, size, frame=frame)
    font = _load_font(lay["font_path"], lay["font_size"])
    lh = _line_height(font)
    h = lh * len(lay["lines"]) + 2 * _stroke_for(lay["font_size"])
    center = int(fm["h"] * POSITIONS.get(pos, POSITIONS["middle"]))
    top = center - h // 2
    lo, hi = fm["safe_top"] + fm["pad"], fm["safe_bottom"] - fm["pad"] - h
    new_top = max(lo, min(top, hi))
    return {"top": new_top, "bottom": new_top + h, "shifted": new_top != top,
            "line_height": lh, "layout": lay}


def render_overlay_image(text: str, pos: str = "middle", size: str = "m",
                         frame=FRAME) -> Image.Image:
    w, h = _frame(frame)
    box = block_box(text, pos, size, frame=(w, h))
    lay = box["layout"]
    fs = lay["font_size"]
    font = _load_font(lay["font_path"], fs)
    stroke = _stroke_for(fs)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    y = box["top"] + stroke
    for line in lay["lines"]:
        draw.text((w / 2, y), line, font=font, anchor="mt",
                  fill=(255, 255, 255, 255), stroke_width=stroke, stroke_fill=(0, 0, 0, 255))
        y += box["line_height"]
    return img


def render_overlay_png(text: str, pos: str = "middle", size: str = "m",
                       out_path: Optional[str] = None, frame=FRAME) -> bytes:
    """PNG bytes of one overlay; also written to `out_path` when given."""
    buf = io.BytesIO()
    render_overlay_image(text, pos, size, frame=frame).save(buf, format="PNG", optimize=True)
    data = buf.getvalue()
    if out_path:
        with open(out_path, "wb") as fh:
            fh.write(data)
    return data


# ---- Validation ----------------------------------------------------------------
def _f(v, default=None):
    try:
        x = float(v)
        return x if x == x else default  # NaN → default
    except Exception:
        return default


def clean_trim(start, end) -> tuple[Optional[float], Optional[float]]:
    """(trim_start, trim_end) in source seconds; None = not trimmed there."""
    s, e = _f(start), _f(end)
    s = round(s, 2) if s is not None and s > 0 else None
    if e is not None and (e <= 0 or e - (s or 0) < 0.5):
        e = None
    return s, (round(e, 2) if e is not None else None)


def clean_overlays(items, clip_duration: Optional[float] = None) -> list[dict]:
    """Validate/clamp overlays from the page: drop empty/invalid ones, cut text
    to MAX_TEXT_LEN, clamp times to [0, clip_duration], keep ≤ MAX_OVERLAYS."""
    out: list[dict] = []
    for it in (items if isinstance(items, list) else []):
        if not isinstance(it, dict):
            continue
        text = re.sub(r"[ \t]+", " ", str(it.get("text") or "")).strip()[:MAX_TEXT_LEN].strip()
        if not text:
            continue
        start = max(0.0, _f(it.get("start"), 0.0))
        end = _f(it.get("end"), start + 2.5)
        if clip_duration and clip_duration > 0:
            start = min(start, clip_duration)
            end = min(end, clip_duration)
        if end - start < 0.1:
            continue
        pos = it.get("pos") if it.get("pos") in POSITIONS else "middle"
        size = it.get("size") if it.get("size") in SIZES else "m"
        out.append({"text": text, "start": round(start, 2), "end": round(end, 2),
                    "pos": pos, "size": size})
        if len(out) >= MAX_OVERLAYS:
            break
    out.sort(key=lambda o: o["start"])
    return out
