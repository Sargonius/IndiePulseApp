"""Repost a published short to more destinations, each with its own caption and language.

The short keeps its one Brief, so its board card, goal and analytics stay together.
Every extra destination (Settings → Languages & destinations) gets a Post with its
destination_id, whose texts live in Post.overrides_json and replace the Brief's at
publish time (platforms.common.brief_for_publish). The publish itself is the usual
pipeline: duplicate guard, {steam} UTM links, size limits, one retry.

Translation (`translate`) also serves the Shorts Factory's "Translate missing".
"""
from __future__ import annotations

import json
import re
from typing import Optional

from loguru import logger
from sqlmodel import select

from app.services import destinations, languages

# Per network: what it shows in one piece (caption + hashtags), for the counter (0 = no
# practical limit; longer texts are not lost: X and Bluesky continue in replies, Telegram
# in follow-up messages) and the hashtags kept by default (0 = the publisher doesn't post them).
SPECS: dict[str, dict] = {
    "instagram": {"limit": 2200, "tags": 3, "note": "Hashtags: 1–3 on-topic ones beat 30 generic ones."},
    "x": {"limit": 280, "tags": 2, "count": "x",
          "note": "A Japanese character counts as 2 of 280. Longer goes out as a thread."},
    "bluesky": {"limit": 300, "tags": 2, "note": "Over 300, the rest goes out as replies."},
    "reddit": {"limit": 300, "tags": 0, "title_only": True,
               "note": "Video with a title, to this destination’s subreddit. The title is everything."},
    "facebook": {"limit": 0, "tags": 3, "note": "A Page video = a Reel (since 2025 every FB video is a Reel)."},
    "vk": {"limit": 0, "tags": 3,
           "note": "Goes out as a video on the group wall. For a Clip: 📋 caption and ⬇ file, upload in the VK app."},
    "telegram": {"limit": 1024, "tags": 0, "note": "No hashtags on Telegram."},
    "discord": {"limit": 2000, "tags": 0,
                "note": "Video over the server limit is compressed automatically (10 MB by default)."},
}

# what a video repost needs besides the destination being set up (destinations.ready)
_REQUIRED = {
    "instagram": ("ig_user_id", "ig_access_token"),
    # tweet via OAuth2, media upload via OAuth1 (a post with media never goes out text-only)
    "x": ("x_api_key", "x_api_secret", "x_oauth_token", "x_oauth_token_secret"),
    # video.save needs the user token, wall.post the group token
    "vk": ("vk_user_access_token",),
}

# post statuses by what they mean for a new repost to the same destination
_LIVE = ("done", "awaiting_finish")
_PENDING = ("queued", "running")


class TranslateError(RuntimeError):
    pass


def _has(cfg: dict, key: str) -> bool:
    return bool(str(cfg.get(key) or "").strip())


def configured(dest: dict, cfg: dict) -> bool:
    """The destination (resolved) is set up and can take a video."""
    return destinations.ready(dest, cfg) and all(_has(cfg, k) for k in _REQUIRED.get(dest["module"], ()))


def targets(cfg: dict) -> list[dict]:
    """The destinations a short can be cross-posted to, in the page's order: turned on, of a module that is on."""
    order = destinations.FORMS["repost"]
    dests = [d for d in destinations.all_destinations(cfg)
             if d["module"] in order and d["enabled"] and destinations.module_on(d)]
    return sorted(dests, key=lambda d: order.index(d["module"]))


def detect_lang(text: str) -> Optional[str]:
    """'ja' | 'ru' | 'en' by script (any kana/kanji = ja), None without letters."""
    from app.services.text_overlay import detect_script
    return {"ja": "ja", "cyr": "ru", "lat": "en"}.get(detect_script(text or ""))


def _real_lang(lang: str, text: str) -> str:
    """The language a text of the brief is in. The factory used to keep a clip's text in text_en whatever its
    language: Japanese or Cyrillic script says so; Latin can be any of many languages, so the key stays."""
    seen = detect_lang(text)
    if seen and seen != lang and (seen in ("ja", "ru") or lang in ("ja", "ru")):
        return seen
    return lang


def source_texts(brief, cfg: Optional[dict] = None) -> dict:
    """What the short went out with on YouTube: title, description, hashtags, language — the description of the
    first language of Settings that has one."""
    title = (brief.title or "").strip()
    desc, key = languages.pick(languages.brief_texts(brief), languages.configured(cfg))
    desc = (desc or "").strip()
    return {
        "title": title,
        "desc": desc,
        "tags": (brief.tags_json or "").strip(),
        "lang": _real_lang(key, f"{title}\n{desc}") if key else (detect_lang(title) or "en"),
    }


def known_texts(session, brief, cfg: Optional[dict] = None) -> dict:
    """{lang: {title, desc, tags}} the short already has, so the page needn't translate them: its own language,
    and the languages the factory wrote (a description on the brief; the title in that language on the post to a
    destination of it, where the brief's own title is in another)."""
    from app.models.models import Post
    from app.platforms.common import post_overrides
    src = source_texts(brief, cfg)
    out = {src["lang"]: {"title": src["title"], "desc": src["desc"], "tags": src["tags"]}}
    own: dict = {}  # lang → the title / hashtags the factory gave a destination of it
    for post in session.exec(select(Post).where(Post.brief_id == brief.id, Post.destination_id.is_not(None))).all():
        dest = destinations.get(post.destination_id, cfg)
        o = post_overrides(post)
        if dest is not None and (o.get("title") or o.get("tags_json")):
            own.setdefault(dest["lang"], {"title": o.get("title") or "", "tags": o.get("tags_json") or ""})
    for lang, text in languages.brief_texts(brief).items():
        text = (text or "").strip()
        real = _real_lang(lang, text)
        if not text or real in out:
            continue
        mine = own.get(real) or {}
        if src["title"] and not mine.get("title"):
            continue  # a description without its title: the page translates the pair instead
        out[real] = {"title": mine.get("title") or "", "desc": text, "tags": mine.get("tags") or ""}
    return out


def _state(status) -> str:
    st = getattr(status, "value", None) or str(status)
    if st in _LIVE:
        return "live"
    if st in _PENDING:
        return "pending"
    return "failed"


def existing_posts(session, brief_id: int, cfg: Optional[dict] = None) -> dict:
    """destination id → the post of this short that matters most for a new repost there (live > pending >
    failed). A post without a destination (from before destinations) counts for the destination its platform's
    old rules sent it to. "yt" → the YouTube post the short started with."""
    from app.models.models import Post
    from app.platforms import platform_key
    rank = {"live": 0, "pending": 1, "failed": 2}
    dests = destinations.all_destinations(cfg)
    best: dict = {}
    for post in session.exec(select(Post).where(Post.brief_id == brief_id).order_by(Post.id.desc())).all():
        plat = platform_key(post.platform)
        if post.destination_id:
            keys = [post.destination_id]
        else:
            keys = [d["id"] for d in dests if destinations.takes_legacy(d, plat)]
        if plat == "yt":
            keys.append("yt")
        for key in keys:
            if key not in best or rank[_state(post.status)] < rank[_state(best[key].status)]:
                best[key] = post
    return best


def page_context(session, brief, cfg: dict) -> dict:
    """Everything the repost page needs, JSON-ready."""
    import os
    posts = existing_posts(session, brief.id, cfg)
    source = source_texts(brief, cfg)
    langs = languages.configured(cfg)
    if source["lang"] not in langs:
        langs = langs + [source["lang"]]  # the short's own language, even when it isn't in Settings
    items = []
    for d in targets(cfg):
        post = posts.get(d["id"])
        item = {"id": d["id"], "module": d["module"], "key": destinations.post_platform(d, "repost"),
                "label": destinations.title(d), "lang": d["lang"], "own_only": d["skip_without_text"],
                "configured": configured(d, cfg), "existing": None, **SPECS[d["module"]]}
        if post is not None:
            item["existing"] = {
                "post_id": post.id, "state": _state(post.status),
                "status": getattr(post.status, "value", None) or str(post.status),
                "url": post.platform_url or "", "error": (post.error_text or "")[:300],
            }
        items.append(item)
    yt = posts.get("yt")
    vertical = (brief.media_vertical_path or "").replace("\\", "/").split("/")[-1]
    poster = ""
    if brief.media_path:
        base = os.path.splitext((brief.media_path or "").replace("\\", "/").split("/")[-1])[0]
        if os.path.exists(os.path.join("media", f"{base}_poster.jpg")):
            poster = f"/media/{base}_poster.jpg"
    return {
        "brief_id": brief.id,
        "source": source,
        "texts": known_texts(session, brief, cfg),
        "langs": [{"code": c, "name": languages.name(c)} for c in langs],
        "video_url": f"/media/{vertical}" if vertical else "",
        "poster_url": poster,
        "yt_url": (yt.platform_url or "") if yt is not None and _state(yt.status) == "live" else "",
        "destinations": items,
    }


def build_overrides(dest: dict, item: dict, brief=None, cfg: Optional[dict] = None) -> dict:
    """The Post.overrides_json for one destination from the page's card; {} = nothing to post.

    The caption goes in as the text of its language and every other language is blanked, so a destination in
    another language still sends this caption (its own language first, then the first, then any text —
    app.services.languages.text_with_lang) and tags it with the language it is really in. A destination that
    takes only its own language gets the caption as that language's text."""
    def text(key: str) -> str:
        return str(item.get(key) or "").strip()

    lang = languages.normalize_code(item.get("lang")) or dest["lang"]
    if dest.get("skip_without_text"):
        lang = dest["lang"]
    blank = set(languages.configured(cfg)) | set(languages.brief_texts(brief)) | {lang, "ru", "en"}
    out = {f"text_{c}": "" for c in sorted(blank)}
    if SPECS[dest["module"]].get("title_only"):
        title = text("title")
        return {"title": title[:300], **out, "tags_json": ""} if title else {}
    body = text("text")
    tags = "" if SPECS[dest["module"]]["tags"] == 0 else text("tags")
    if not (body or tags):
        return {}
    out[f"text_{lang}"] = body
    out["tags_json"] = tags
    if text("title"):
        out["title"] = text("title")
    return out


def _destination_of(item: dict, dests: list[dict]) -> Optional[dict]:
    """The destination a card names; an older caller's `platform`: the destination its old rules went to."""
    did = str(item.get("destination") or "")
    if did:
        return next((d for d in dests if d["id"] == did), None)
    plat = str(item.get("platform") or "")
    return next((d for d in dests if plat and destinations.takes_legacy(d, plat)), None)


def create_reposts(session, brief, items: list, allow_duplicate: bool = False, cfg: Optional[dict] = None) -> dict:
    """Queue the short on the picked destinations right now.

    A destination that already has this short (live or on its way) is skipped unless
    `allow_duplicate`, which also lets the post through the worker's duplicate guard.
    Raises ValueError when the short has no rendered vertical file yet.
    """
    from app.jobs.tasks import publish_news_post
    from app.models.models import Job, JobKindEnum, JobStatusEnum, PlatformEnum, Post, PostStatusEnum
    from app.services import duplicates
    from app.services.settings import load_settings
    cfg = cfg if cfg is not None else load_settings()
    media = (brief.media_vertical_path or "").strip()
    if not media:
        raise ValueError("not_rendered")
    every = destinations.all_destinations(cfg)
    usable = {d["id"] for d in targets(cfg)}
    posts = existing_posts(session, brief.id, cfg)
    created, skipped, queued, seen = [], [], [], set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        dest = _destination_of(item, every)
        name = dest["id"] if dest else str(item.get("destination") or item.get("platform") or "")
        if dest is None or dest["id"] in seen:
            skipped.append({"destination": name, "reason": "unknown_destination" if dest is None else "twice"})
            continue
        seen.add(dest["id"])
        if dest["id"] not in usable:
            skipped.append({"destination": name, "reason": "turned_off"})
            continue
        if not configured(dest, cfg):
            skipped.append({"destination": name, "reason": "not_configured"})
            continue
        prev = posts.get(dest["id"])
        if prev is not None and _state(prev.status) != "failed" and not allow_duplicate:
            skipped.append({"destination": name, "reason": "already", "post_id": prev.id})
            continue
        overrides = build_overrides(dest, item, brief, cfg)
        if not overrides:
            skipped.append({"destination": name, "reason": "empty_text"})
            continue
        plat = destinations.post_platform(dest, "repost")
        post = Post(user_id=1, brief_id=brief.id, platform=PlatformEnum(plat), destination_id=dest["id"],
                    status=PostStatusEnum.queued, overrides_json=json.dumps(overrides, ensure_ascii=False))
        session.add(post)
        session.commit()
        session.refresh(post)
        payload = {"post_id": post.id, "media_rel": media, "repost": True}
        if allow_duplicate:
            payload[duplicates.ALLOW] = True
        job = Job(kind=JobKindEnum.publish, payload_json=json.dumps(payload, ensure_ascii=False),
                  status=JobStatusEnum.queued)
        session.add(job)
        session.commit()
        session.refresh(job)
        queued.append((job.id, post.id))
        created.append({"destination": dest["id"], "platform": plat, "post_id": post.id})
    # schedule after every row is committed: the worker may pick a job up at once
    for job_id, post_id in queued:
        publish_news_post.schedule(args=(job_id, post_id), delay=0)
    logger.info(f"repost brief={brief.id} created={[c['destination'] for c in created]} "
                f"skipped={[(s['destination'], s['reason']) for s in skipped]}")
    return {"created": created, "skipped": skipped}


# ── translation ──────────────────────────────────────────────────────────────

_SYSTEM = (
    "You localize the title, description and hashtags of a short gameplay video that an indie "
    "game developer posts on social networks. Translate everything into {lang}.\n"
    "- Write the way a native {lang}-speaking indie developer posts on social media: natural and "
    "short, never word-for-word. The title is a hook: keep it punchy and about as short as the original.\n"
    "- Keep unchanged: game titles and other proper names, URLs, @handles, emoji, line breaks and the "
    "literal placeholder {{steam}}.\n"
    "- Hashtags: give the ones {lang}-speaking players and developers really use for the same topics "
    "(for Japanese e.g. #indiegame → #インディーゲーム, #gamedev → #ゲーム開発), no more hashtags than "
    "the original. Keep #Shorts and hashtags of game names as they are.\n"
    "- An empty field stays empty. Text already in {lang} stays as it is.\n"
    'Return ONLY a JSON object: {{"title": "...", "desc": "...", "tags": "..."}}'
)


def normalize_tags(tags: str) -> str:
    """'#a, b  #c' → '#a #b #c' (hashtags can't hold spaces or commas)."""
    out = []
    for tok in re.split(r"[\s,、，]+", tags or ""):
        tok = tok.strip()
        if not tok or tok == "#":
            continue
        tok = "#" + tok.lstrip("#")
        if tok.lower() not in (t.lower() for t in out):
            out.append(tok)
    return " ".join(out)


def translate(fields: dict, target: str) -> dict:
    """{title, desc, tags} → the same in `target` (any language code: 'ja', 'de', 'pt-br'...) + provider.

    The AI from Settings first (OpenAI or Claude); without a key or on failure, machine
    translation (DeepL or Google) for the title and description (hashtags stay as they
    are). Raises TranslateError when neither works.
    """
    from app.services import llm, machine_translate
    code = languages.normalize_code(target)
    if code is None:
        raise ValueError(f"unknown language: {target}")
    target = code
    src = {k: str(fields.get(k) or "").strip() for k in ("title", "desc", "tags")}
    if not any(src.values()):
        return {**src, "provider": ""}
    lang = languages.name(target)
    try:
        content, provider = llm.chat(
            [{"role": "system", "content": _SYSTEM.format(lang=lang)},
             {"role": "user", "content": json.dumps(src, ensure_ascii=False)}],
            temperature=0.3, timeout=60)
        data = llm.json_object(content)
        if data is not None:
            return {
                "title": str(data.get("title") or "").strip() if src["title"] else "",
                "desc": str(data.get("desc") or "").strip() if src["desc"] else "",
                "tags": normalize_tags(str(data.get("tags") or "")) if src["tags"] else "",
                "provider": provider,
            }
        problem = f"{provider}: not a JSON answer"
    except llm.LLMError as exc:
        problem = str(exc)
        logger.warning(f"repost translate ai: {exc}")
    out, used = {"tags": src["tags"]}, ""
    for field in ("title", "desc"):
        out[field], name = machine_translate.translate(src[field], target) if src[field] else ("", "")
        used = used or name
        if src[field] and not out[field]:
            raise TranslateError(f"translation failed ({problem}; machine translation: nothing worked)")
    return {**out, "provider": used}
