"""Shorts Factory checklist ("don’t screw it up"): pure checks of one clip against
the rules in guide.html (#anatomy, #lang, #checklist). Nothing here blocks a
submit — the page shows the findings and asks once before sending.

A finding is {code, level, message}; level is ok | info | warn | error.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Iterable, Optional

from app.services.text_overlay import POSITIONS, SIZES, block_box, detect_script

HOOK_WINDOW = 0.5      # a text line must start this early to count as the hook
END_WINDOW = 4.0       # the game name must be on screen within the last N seconds
MIN_ON_SCREEN = 1.2
TITLE_VISIBLE_LATIN = 40
TITLE_VISIBLE_JA = 20
NEAR_DUPLICATE = 0.85

LEVEL_ORDER = {"error": 0, "warn": 1, "info": 2, "ok": 3}
FADE_IN_MAX = 0.3      # s of audio fade-in before "no sound on the first frame"

# Titles about the work instead of the game ("Prototyping…", "More layout changes",
# "Devlog #12"): they promise the viewer nothing. A story hook like "I rebuilt combat
# after one review" stays allowed, so only process jargon is listed.
_DEV_TITLE_RE = re.compile(
    r"\b(prototyp\w*|layouts?|wip|devlog\w*|dev log|tweak\w*|refactor\w*|polish(?:ing|ed)?|"
    r"iterations?|(?:initial|first|second|final) pass|working on|rebuilding|remodel\w*|"
    r"blockout|block-out|gr[ae]ybox\w*|placeholders?|прототип\w*|девлог\w*|правк\w*|"
    r"доработк\w*|переделк\w*|работаю над)\b"
    r"|作業|開発中|進捗|試作|実装中|プロトタイプ|レイアウト",
    re.I)


def _f(code: str, level: str, message: str) -> dict:
    return {"code": code, "level": level, "message": message}


def _num(v) -> Optional[float]:
    try:
        x = float(v)
        return x if x == x else None
    except Exception:
        return None


def normalize(text: str) -> str:
    """Lowercase, no hashtags/punctuation/spaces — for "is this the same hook"."""
    t = re.sub(r"#\S+", " ", (text or "").lower())
    return re.sub(r"[\W_]+", "", t)


def _similar(a: str, b: str) -> bool:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return False
    return a == b or SequenceMatcher(None, a, b).ratio() >= NEAR_DUPLICATE


def _words(text: str) -> int:
    return len([w for w in re.split(r"\s+", (text or "").strip()) if re.search(r"\w", w)])


def _contains_name(text: str, name: str) -> bool:
    n = normalize(name)
    return bool(n) and n in normalize(text)


def _strip_common(text: str, project_name: str) -> str:
    """Drop the game name and the Steam call-to-action before comparing
    scripts: 'Project Apocalypse — Wishlist on Steam' is fine on any clip."""
    t = text or ""
    if project_name:
        t = re.sub(re.escape(project_name), " ", t, flags=re.I)
    return re.sub(r"wishlist|on steam|steam", " ", t, flags=re.I)


def _fmt(s: float) -> str:
    return f"{s:.1f}".rstrip("0").rstrip(".")


def _lines(ids: list, one: str, many: str) -> str:
    """'Line 2 <one>' / 'Lines 1, 3 <many>' for findings about overlay lines."""
    return f"Line {ids[0]} {one}" if len(ids) == 1 else f"Lines {', '.join(map(str, ids))} {many}"


def _n(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def lint_clip(
    *,
    title: str = "",
    description: str = "",
    tags: str = "",
    duration: Optional[float] = None,
    overlays: Optional[list] = None,
    project_name: str = "",
    goal_id=None,
    platforms: Iterable[str] = (),
    music_path: str = "",
    batch_titles: Iterable[str] = (),
    batch_hooks: Iterable[str] = (),
    recent_titles: Iterable[str] = (),
    fade_in=None,
) -> list[dict]:
    """All checks for one clip. `duration` is after the trim (None = unknown);
    `overlays` are the raw items from the page (times relative to the trim);
    batch_* come from the other cards of the same batch, recent_titles are
    the last titles already published for this project."""
    out: list[dict] = []
    title = (title or "").strip()
    project_name = (project_name or "").strip()
    items = [o for o in (overlays or []) if isinstance(o, dict) and str(o.get("text") or "").strip()]
    lines = []
    for o in items:
        s, e = _num(o.get("start")), _num(o.get("end"))
        lines.append({"text": str(o.get("text")).strip(), "start": s if s is not None else 0.0,
                      "end": e if e is not None else 0.0, "pos": o.get("pos"), "size": o.get("size")})
    lines.sort(key=lambda o: o["start"])
    dur = _num(duration)

    # 1. Duration after trim
    if dur is None or dur <= 0:
        out.append(_f("duration", "info", "Length unknown — open the editor so the video loads"))
    elif dur < 5 or dur > 60:
        out.append(_f("duration", "error", f"Length {_fmt(dur)} s — needs 12–25 s. Trim it or pick another part"))
    elif 12 <= dur <= 25:
        out.append(_f("duration", "ok", f"Length {_fmt(dur)} s"))
    else:
        tip = "trim to 12–25 s" if dur > 25 else "12–25 s is better; add more footage"
        out.append(_f("duration", "warn", f"Length {_fmt(dur)} s — {tip}"))

    # 2. Hook: a text line in the first 0.5 s
    hook = next((o for o in lines if o["start"] <= HOOK_WINDOW), None)
    if hook is None:
        out.append(_f("hook_missing", "warn", "No hook line in the first 0.5 s — add “＋ Hook”"))
    else:
        out.append(_f("hook_missing", "ok", "Hook from the first frame"))
        # 3. Hook length
        if detect_script(hook["text"]) == "ja":
            n = len(re.sub(r"\s", "", hook["text"]))
            if n > 20:
                out.append(_f("hook_length", "warn", f"Hook is {_n(n, 'character')} — keep Japanese ≤ 20, shorten it"))
            else:
                out.append(_f("hook_length", "ok", f"Hook: {_n(n, 'character')}"))
        else:
            n = _words(hook["text"])
            if n < 3 or n > 7:
                out.append(_f("hook_length", "warn", f"Hook is {_n(n, 'word')} — needs 3–7, " + ("shorten it" if n > 7 else "add more")))
            else:
                out.append(_f("hook_length", "ok", f"Hook: {n} words"))

    # 4. Every line fits 2 lines and stays in the safe zone
    if lines:
        too_long, bad_pos = [], []
        for i, o in enumerate(lines, 1):
            if o["pos"] not in POSITIONS or (o["size"] not in SIZES):
                bad_pos.append(i)
            try:
                box = block_box(o["text"], o["pos"] if o["pos"] in POSITIONS else "middle",
                                o["size"] if o["size"] in SIZES else "m")
                if not box["layout"]["fits"]:
                    too_long.append(i)
            except RuntimeError:  # no font on this machine: nothing to measure
                pass
        if too_long:
            out.append(_f("text_fit", "warn", _lines(too_long, "doesn’t fit in 2 rows", "don’t fit in 2 rows") + " — shorten or use a smaller size"))
        else:
            out.append(_f("text_fit", "ok", "All text fits in 2 rows"))
        if bad_pos:
            out.append(_f("text_safe_zone", "error", _lines(bad_pos, "is", "are") + " outside the safe zone — pick top/upper/middle/lower"))
        else:
            out.append(_f("text_safe_zone", "ok", "Text is in the safe zone"))

    # 5. No overlap at the same position; each line on screen ≥ 1.2 s
    if lines:
        overlap = []
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i], lines[j]
                if a["pos"] == b["pos"] and a["start"] < b["end"] and b["start"] < a["end"]:
                    overlap.append(f"{i + 1}+{j + 1}")
        if overlap:
            out.append(_f("text_overlap", "warn", f"Lines {', '.join(overlap)} overlap in the same spot — separate them in time or position"))
        short = [str(i) for i, o in enumerate(lines, 1) if o["end"] - o["start"] < MIN_ON_SCREEN]
        late = [str(i) for i, o in enumerate(lines, 1) if dur and o["start"] >= dur]
        if short:
            out.append(_f("text_short", "warn", _lines(short, "is", "are") + f" on screen under {_fmt(MIN_ON_SCREEN)} s — too fast to read"))
        if late:
            out.append(_f("text_after_end", "warn", _lines(late, "starts after the clip ends — it won’t", "start after the clip ends — they won’t") + " make it into the video"))
        if not overlap and not short and not late:
            out.append(_f("text_timing", "ok", "Line timing is fine"))

    # 6. Game name: on screen at the end and in the title
    if not project_name:
        out.append(_f("name_skip", "info", "No project selected — game name check skipped"))
    else:
        if dur:
            at_end = any(_contains_name(o["text"], project_name) and o["end"] > dur - END_WINDOW for o in lines)
        else:
            at_end = any(_contains_name(o["text"], project_name) for o in lines)
        if at_end:
            out.append(_f("name_end", "ok", "Game name on screen at the end"))
        else:
            out.append(_f("name_end", "warn", f"No “{project_name}” on screen in the last {int(END_WINDOW)} s — add “＋ End title”"))
        if _contains_name(title, project_name):
            out.append(_f("name_title", "ok", "Game name in the title"))
        else:
            out.append(_f("name_title", "warn", f"No “{project_name}” in the title — it’s the only “link” for finding the game on Steam"))

    # 7. Title
    if not title:
        out.append(_f("title_empty", "error", "Title is empty — the placeholder “IndiePulse Short” will go out"))
    else:
        ja = detect_script(title) == "ja"
        visible = TITLE_VISIBLE_JA if ja else TITLE_VISIBLE_LATIN
        dev = _DEV_TITLE_RE.search(title)
        if title.startswith("#"):
            out.append(_f("title_hashtag", "warn", "Title starts with a hashtag — lead with the hook and the game name"))
        elif dev:
            out.append(_f("title_dev", "warn", f"Title is about the dev process (“{dev.group(0)}”) — "
                                               "write about a feeling or a moment in the game: a question, a conflict, “Day 1 vs Day 30”"))
        elif (not ja and _words(title) <= 2) or (ja and len(title) <= 6):
            out.append(_f("title_label", "warn", "Title reads like a process label — say what the viewer gets"))
        else:
            out.append(_f("title_ok", "ok", "Title has a hook"))
        if len(title) > visible:
            out.append(_f("title_visible", "info", f"The player shows ~{visible} characters: “{title[:visible]}…”"))

    # 7b. Description: not empty, and not the title again (TikTok's caption is
    # title + description + hashtags, so a copy shows twice)
    desc = re.sub(r"\{steam\}|https?://\S+", " ", description or "").strip()
    if not desc:
        out.append(_f("desc_empty", "warn", "Description is empty — YouTube gets “Uploaded via IndiePulse”. "
                                            "Add 1–2 sentences: what the game is and what’s in the clip"))
    elif normalize(title) and (_similar(desc, title) or normalize(desc).startswith(normalize(title))):
        out.append(_f("desc_repeat", "warn", "Description repeats the title — the TikTok caption shows them back to back. "
                                             "Add what the game is and what’s happening"))
    else:
        out.append(_f("desc_ok", "ok", "Description adds to the title"))

    # 8. Language of the title = language of the text on screen
    t_script = detect_script(_strip_common(title, project_name))
    if t_script and lines:
        names = {"lat": "Latin script", "cyr": "Cyrillic", "ja": "Japanese"}
        bad = [str(i) for i, o in enumerate(lines, 1)
               if (s := detect_script(_strip_common(o["text"], project_name))) and s != t_script]
        if bad:
            out.append(_f("lang_mismatch", "warn", f"Title is in {names[t_script]}, but " + _lines(bad, "is", "are").lower() + " in another language — use one"))
        else:
            out.append(_f("lang_mismatch", "ok", "Title and on-screen text use the same language"))

    # 9. Goal
    if goal_id in (None, "", 0, "0"):
        out.append(_f("goal_missing", "warn", "No goal selected — link the clip to a campaign, or you won’t know what worked"))
    else:
        out.append(_f("goal_missing", "ok", "Goal selected"))

    # 10. Repetition within the batch and vs. the last published titles
    recent = [t for t in recent_titles if t][:3]
    rep = []
    hook_text = hook["text"] if hook else ""
    if title and any(_similar(title, t) for t in batch_titles):
        rep.append("title repeats another clip in the batch")
    if hook_text and any(_similar(hook_text, h) for h in batch_hooks):
        rep.append("hook repeats another clip in the batch")
    if (title and any(_similar(title, t) for t in recent)) or (hook_text and any(_similar(hook_text, t) for t in recent)):
        rep.append("matches a recent video of this project")
    if rep:
        out.append(_f("repeat", "warn", "Repeat: " + "; ".join(rep) + " — change the hook"))
    else:
        out.append(_f("repeat", "ok", "Hook and title aren’t repeats"))

    # 11. Music for YouTube
    if not music_path and "yt" in set(platforms or ()):
        out.append(_f("music_youtube", "info", "No music: for YouTube it’s baked in at render (TikTok — add a sound in the app)"))

    # 12. Sound from the first frame: the page sends the fade-in only when the music
    # is the whole sound (the game's own sound muted or absent)
    fade = _num(fade_in)
    if fade is not None and fade > FADE_IN_MAX:
        out.append(_f("fade_in", "warn", f"Sound fades in over {_fmt(fade)} s — the first frame should be loud right away "
                                         "(a shot, an engine, music). Editor → Sound → “fade in” 0"))

    out.sort(key=lambda f: LEVEL_ORDER.get(f["level"], 9))
    return out


def summarize(findings: list[dict]) -> dict:
    counts = {"ok": 0, "info": 0, "warn": 0, "error": 0}
    for f in findings:
        counts[f["level"]] = counts.get(f["level"], 0) + 1
    return counts


def lint_batch(cards: list, *, project_name: str = "", goal_id=None, platforms=(),
               recent_titles=(), fade_in=None) -> list[dict]:
    """Lint every card of a batch; each card is compared with the others. `fade_in`
    is the batch default; a card's own "fade_in" replaces it."""
    cards = [c if isinstance(c, dict) else {} for c in (cards or [])]

    def _hook(c):
        items = sorted((o for o in (c.get("overlays") or []) if isinstance(o, dict) and str(o.get("text") or "").strip()),
                       key=lambda o: _num(o.get("start")) or 0.0)
        return str(items[0]["text"]) if items and (_num(items[0].get("start")) or 0.0) <= HOOK_WINDOW else ""

    titles = [(c.get("title") or "").strip() for c in cards]
    hooks = [_hook(c) for c in cards]
    result = []
    for i, c in enumerate(cards):
        findings = lint_clip(
            title=titles[i], description=c.get("description") or "", tags=c.get("tags") or "",
            duration=c.get("duration"), overlays=c.get("overlays") or [],
            project_name=project_name, goal_id=goal_id, platforms=platforms,
            music_path=c.get("music_path") or "",
            batch_titles=[t for j, t in enumerate(titles) if j != i],
            batch_hooks=[h for j, h in enumerate(hooks) if j != i],
            recent_titles=recent_titles,
            fade_in=c["fade_in"] if "fade_in" in c else fade_in,  # a card's own wins (its music)
        )
        result.append({"findings": findings, "counts": summarize(findings)})
    return result
