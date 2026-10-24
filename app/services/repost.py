"""Repost a published short to more networks, each with its own caption and language.

The short keeps its one Brief, so its board card, goal and analytics stay together.
Every extra network gets a Post whose texts live in Post.overrides_json and replace
the Brief's at publish time (platforms.common.brief_for_publish). The publish itself
is the usual pipeline: duplicate guard, {steam} UTM links, size limits, one retry.

Translation (`translate`) also serves the Shorts Factory's per-clip EN/JA/RU buttons.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from loguru import logger
from sqlmodel import select

LANGS = ("en", "ja", "ru")
LANG_NAMES = {"en": "English", "ja": "Japanese", "ru": "Russian"}

# lang: "src" = the short's own language, "en"/"ru" = always that language,
#       "dual" = Discord: one text per configured webhook (RU and EN channels).
# limit: what the network shows in one piece (caption + hashtags), for the counter;
#        0 = no practical limit. Longer texts are not lost: X and Bluesky continue in
#        replies, Telegram in follow-up messages.
# tags: hashtags kept by default (0 = the publisher doesn't post them).
PLATFORMS: dict[str, dict] = {
    "ig_reels": {"label": "Instagram Reels", "lang": "src", "limit": 2200, "tags": 3,
                 "note": "Hashtags: 1–3 on-topic ones beat 30 generic ones."},
    "x": {"label": "X", "lang": "src", "limit": 280, "tags": 2, "count": "x",
          "note": "A Japanese character counts as 2 of 280. Longer goes out as a thread."},
    "bluesky": {"label": "Bluesky", "lang": "src", "limit": 300, "tags": 2,
                "note": "Over 300, the rest goes out as replies."},
    "reddit": {"label": "Reddit", "lang": "en", "limit": 300, "tags": 0, "title_only": True,
               "note": "Video with a title, to the subreddit from Settings. The title is everything."},
    "fb_en": {"label": "Facebook EN", "lang": "en", "limit": 0, "tags": 3,
              "note": "A Page video = a Reel (since 2025 every FB video is a Reel)."},
    "fb_ru": {"label": "Facebook RU", "lang": "ru", "limit": 0, "tags": 3,
              "note": "A Page video = a Reel (since 2025 every FB video is a Reel)."},
    "vk_video": {"label": "VK", "lang": "ru", "limit": 0, "tags": 3,
                 "note": "Goes out as a video on the group wall. For a Clip: 📋 caption and ⬇ file, upload in the VK app."},
    "tg": {"label": "Telegram", "lang": "ru", "limit": 1024, "tags": 0,
           "note": "No hashtags on Telegram."},
    "discord": {"label": "Discord", "lang": "dual", "limit": 2000, "tags": 0,
                "note": "Video over the server limit is compressed automatically (10 MB by default)."},
}

_REQUIRED = {
    "ig_reels": ("ig_user_id", "ig_access_token"),
    # tweet via OAuth2, media upload via OAuth1 (a post with media never goes out text-only)
    "x": ("x_api_key", "x_api_secret", "x_oauth_token", "x_oauth_token_secret"),
    "bluesky": ("bluesky_handle", "bluesky_app_password"),
    "reddit": ("reddit_client_id", "reddit_client_secret", "reddit_username", "reddit_password",
               "reddit_subreddit_en"),
    "fb_en": ("fb_en_page_id", "fb_en_page_access_token"),
    "fb_ru": ("fb_ru_page_id", "fb_ru_page_access_token"),
    # video.save needs the user token, wall.post the group token
    "vk_video": ("vk_access_token", "vk_group_id", "vk_user_access_token"),
    "tg": ("tg_bot_token", "tg_chat_id_ru"),
}

# post statuses by what they mean for a new repost to the same network
_LIVE = ("done", "awaiting_finish")
_PENDING = ("queued", "running")


class TranslateError(RuntimeError):
    pass


def _has(cfg: dict, key: str) -> bool:
    return bool(str(cfg.get(key) or "").strip())


def discord_langs(cfg: dict) -> list[str]:
    """Discord channels a repost reaches: 'ru'/'en' webhooks, or ['default']."""
    langs = [lang for lang in ("ru", "en") if _has(cfg, f"discord_webhook_{lang}")]
    if not langs and _has(cfg, "discord_webhook_url"):
        return ["default"]
    return langs


def configured(platform: str, cfg: dict) -> bool:
    if platform == "discord":
        return bool(discord_langs(cfg))
    if platform == "x" and not (_has(cfg, "x_access_token") or _has(cfg, "x_refresh_token")):
        return False
    return all(_has(cfg, k) for k in _REQUIRED.get(platform, ()))


def detect_lang(text: str) -> Optional[str]:
    """'ja' | 'ru' | 'en' by script (any kana/kanji = ja), None without letters."""
    from app.services.text_overlay import detect_script
    return {"ja": "ja", "cyr": "ru", "lat": "en"}.get(detect_script(text or ""))


def source_texts(brief) -> dict:
    """What the short went out with on YouTube: title, description, hashtags, language."""
    title = (brief.title or "").strip()
    desc = (brief.text_en or brief.text_ru or "").strip()
    return {
        "title": title,
        "desc": desc,
        "tags": (brief.tags_json or "").strip(),
        "lang": detect_lang(f"{title}\n{desc}") or "en",
    }


def _state(status) -> str:
    st = getattr(status, "value", None) or str(status)
    if st in _LIVE:
        return "live"
    if st in _PENDING:
        return "pending"
    return "failed"


def existing_posts(session, brief_id: int) -> dict:
    """platform → the post that matters most for a new repost (live > pending > failed)."""
    from app.models.models import Post
    from app.platforms import platform_key
    rank = {"live": 0, "pending": 1, "failed": 2}
    best: dict = {}
    for post in session.exec(select(Post).where(Post.brief_id == brief_id).order_by(Post.id.desc())).all():
        key = platform_key(post.platform)
        if key not in best or rank[_state(post.status)] < rank[_state(best[key].status)]:
            best[key] = post
    return best


def page_context(session, brief, cfg: dict) -> dict:
    """Everything the repost page needs, JSON-ready."""
    import os
    posts = existing_posts(session, brief.id)
    platforms = []
    for key, spec in PLATFORMS.items():
        post = posts.get(key)
        item = {"key": key, **spec, "configured": configured(key, cfg), "existing": None}
        if key == "discord":
            langs = discord_langs(cfg)
            item["dual_langs"] = langs
            if langs == ["default"]:  # one webhook: the publisher sends text_en or text_ru
                item["lang"] = "src"
        if post is not None:
            item["existing"] = {
                "post_id": post.id, "state": _state(post.status),
                "status": getattr(post.status, "value", None) or str(post.status),
                "url": post.platform_url or "", "error": (post.error_text or "")[:300],
            }
        platforms.append(item)
    yt = posts.get("yt")
    vertical = (brief.media_vertical_path or "").replace("\\", "/").split("/")[-1]
    poster = ""
    if brief.media_path:
        base = os.path.splitext((brief.media_path or "").replace("\\", "/").split("/")[-1])[0]
        if os.path.exists(os.path.join("media", f"{base}_poster.jpg")):
            poster = f"/media/{base}_poster.jpg"
    return {
        "brief_id": brief.id,
        "source": source_texts(brief),
        "video_url": f"/media/{vertical}" if vertical else "",
        "poster_url": poster,
        "yt_url": (yt.platform_url or "") if yt is not None and _state(yt.status) == "live" else "",
        "platforms": platforms,
    }


def build_overrides(platform: str, item: dict, cfg: dict) -> dict:
    """The Post.overrides_json for one network from the page's card; {} = nothing to post."""
    def text(key: str) -> str:
        return str(item.get(key) or "").strip()

    if platform == "reddit":
        title = text("title")
        return {"title": title[:300], "text_en": "", "text_ru": "", "tags_json": ""} if title else {}
    if platform == "discord":
        langs = discord_langs(cfg)
        if langs == ["default"]:
            body = text("text")
            return {"text_en": body, "text_ru": "", "tags_json": ""} if body else {}
        out = {"text_ru": text("text_ru") if "ru" in langs else "",
               "text_en": text("text_en") if "en" in langs else "", "tags_json": ""}
        return out if (out["text_ru"] or out["text_en"]) else {}
    body = text("text")
    tags = "" if PLATFORMS[platform]["tags"] == 0 else text("tags")
    if not (body or tags):
        return {}
    # the publishers read text_ru first (VK, Telegram, FB RU) or text_en first (the rest)
    # and fall back to the other one, so only the field of the caption's language is set
    if item.get("lang") == "ru":
        out = {"text_ru": body, "text_en": ""}
    else:
        out = {"text_en": body, "text_ru": ""}
    out["tags_json"] = tags
    if text("title"):
        out["title"] = text("title")
    return out


def create_reposts(session, brief, items: list, allow_duplicate: bool = False, cfg: Optional[dict] = None) -> dict:
    """Queue the short on the picked networks right now.

    A network that already has this short (live or on its way) is skipped unless
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
    posts = existing_posts(session, brief.id)
    created, skipped, queued, seen = [], [], [], set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        plat = str(item.get("platform") or "")
        if plat not in PLATFORMS or plat in seen:
            skipped.append({"platform": plat, "reason": "unknown_platform" if plat not in PLATFORMS else "twice"})
            continue
        seen.add(plat)
        if not configured(plat, cfg):
            skipped.append({"platform": plat, "reason": "not_configured"})
            continue
        prev = posts.get(plat)
        if prev is not None and _state(prev.status) != "failed" and not allow_duplicate:
            skipped.append({"platform": plat, "reason": "already", "post_id": prev.id})
            continue
        overrides = build_overrides(plat, item, cfg)
        if not overrides:
            skipped.append({"platform": plat, "reason": "empty_text"})
            continue
        post = Post(user_id=1, brief_id=brief.id, platform=PlatformEnum(plat), status=PostStatusEnum.queued,
                    overrides_json=json.dumps(overrides, ensure_ascii=False))
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
        created.append({"platform": plat, "post_id": post.id})
    # schedule after every row is committed: the worker may pick a job up at once
    for job_id, post_id in queued:
        publish_news_post.schedule(args=(job_id, post_id), delay=0)
    logger.info(f"repost brief={brief.id} created={[c['platform'] for c in created]} "
                f"skipped={[(s['platform'], s['reason']) for s in skipped]}")
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


def _parse_json_object(content: str) -> Optional[dict]:
    raw = (content or "").strip()
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
    for candidate in (raw, (re.search(r"\{.*\}", raw, re.S) or [None])[0]):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


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
    """{title, desc, tags} → the same in `target` ('en' | 'ja' | 'ru') + provider.

    OpenAI first (settings model); without a key or on failure, Google Translate for
    the title and description (hashtags stay as they are). Raises TranslateError when
    neither works.
    """
    from app.services import openai_client
    if target not in LANGS:
        raise ValueError(f"unknown language: {target}")
    src = {k: str(fields.get(k) or "").strip() for k in ("title", "desc", "tags")}
    if not any(src.values()):
        return {**src, "provider": ""}
    lang = LANG_NAMES[target]
    problem = ""
    try:
        key, _ = openai_client.settings()
        if key:
            user = json.dumps(src, ensure_ascii=False)
            resp, model = openai_client.chat_completion(
                [{"role": "system", "content": _SYSTEM.format(lang=lang)},
                 {"role": "user", "content": user}],
                temperature=0.3, timeout=60)
            if resp.status_code < 400:
                data = _parse_json_object(openai_client.content_of(resp))
                if data is not None:
                    return {
                        "title": str(data.get("title") or "").strip() if src["title"] else "",
                        "desc": str(data.get("desc") or "").strip() if src["desc"] else "",
                        "tags": normalize_tags(str(data.get("tags") or "")) if src["tags"] else "",
                        "provider": f"openai:{model}",
                    }
                problem = "openai: not a JSON answer"
            else:
                problem = f"openai: HTTP {resp.status_code}"
                logger.warning(f"repost translate openai status={resp.status_code} body={resp.text[:300]}")
        else:
            problem = "no OpenAI key"
    except Exception as exc:
        problem = f"openai: {exc}"
        logger.warning(f"repost translate openai error: {exc}")
    try:
        from deep_translator import GoogleTranslator
        gt = GoogleTranslator(source="auto", target=target)
        return {
            "title": (gt.translate(src["title"]) or "").strip() if src["title"] else "",
            "desc": (gt.translate(src["desc"]) or "").strip() if src["desc"] else "",
            "tags": src["tags"],
            "provider": "google",
        }
    except Exception as exc:
        logger.warning(f"repost translate fallback error: {exc}")
        raise TranslateError(f"translation failed ({problem}; google: {exc})")
