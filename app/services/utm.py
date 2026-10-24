"""UTM links for Steam store pages (Steamworks → UTM Analytics).

Pure logic: slugs, the platform/placement registry, URL building and the {steam}
placeholder. Storage (UtmLink) and pages live in the marketing routers.
Steam reads utm_source / utm_medium / utm_campaign / utm_term / utm_content.
"""
from __future__ import annotations

import re
import unicodedata
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SLUG_MAX = 40

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh",
    "щ": "shch", "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    # Ukrainian / Belarusian extras
    "і": "i", "ї": "yi", "є": "ye", "ґ": "g", "ў": "u",
}


def slug(value) -> str:
    """"Патч Октябрь!" → "patch_oktyabr". Only [a-z0-9_-]; text with no Latin or
    Cyrillic letters/digits (e.g. Japanese) becomes ""."""
    text = "".join(_TRANSLIT.get(ch, ch) for ch in str(value or "").lower())
    # é → e, fullwidth Ａ → a; what is still non-ASCII is dropped below
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9-]+", "_", text)
    text = re.sub(r"[-_]*_[-_]*", "_", text)  # "a - b" → "a_b"
    text = re.sub(r"-{2,}", "-", text)
    return text[:SLUG_MAX].strip("_-")


# ── platforms and where the link goes ────────────────────────────────────────
PLACEMENT_LABELS = {
    "bio": "Profile / bio",
    "shorts": "Short / clip",
    "video": "Video (description)",
    "post": "Post",
    "comment": "Comment",
    "description": "Page description",
    "story": "Story",
}


def _pl(key: str, clickable: Optional[bool], hint: str) -> dict:
    return {"key": key, "label": PLACEMENT_LABELS[key], "clickable": clickable, "hint": hint}


# clickable: True — link works, False — plain text only, None — depends / check yourself
SOURCES: dict[str, dict] = {
    "youtube": {"label": "YouTube", "placements": [
        _pl("bio", True, "The link in the channel header is clickable — the main spot for Shorts viewers."),
        _pl("shorts", False, "Links in a Short’s description aren’t clickable: the tag only counts if a viewer copies the link."),
        _pl("video", True, "In a regular (long) video’s description the link is clickable."),
        _pl("comment", None, "Links in comments aren’t clickable under Shorts, but are under long videos (pin the comment)."),
    ]},
    "tiktok": {"label": "TikTok", "placements": [
        _pl("bio", True, "Only the bio link is clickable (needs a business account or 1,000+ followers)."),
        _pl("shorts", False, "Links in a video caption aren’t clickable."),
        _pl("comment", False, "Links in comments aren’t clickable."),
    ]},
    "instagram": {"label": "Instagram", "placements": [
        _pl("bio", True, "Only profile links are clickable (bio / “Links”)."),
        _pl("story", True, "The “Link” sticker in a story is clickable."),
        _pl("shorts", False, "Links in a Reels caption aren’t clickable."),
        _pl("comment", False, "Links in comments aren’t clickable."),
    ]},
    "vk": {"label": "VK", "placements": [
        _pl("post", True, "A link in a post is clickable."),
        _pl("video", True, "A link in a video / clip description is clickable."),
        _pl("bio", True, "A link in the community description / links is clickable."),
        _pl("comment", True, "A link in a comment is clickable."),
    ]},
    "x": {"label": "X (Twitter)", "placements": [
        _pl("post", True, "A link in a post is clickable (X trims the reach of posts with links a bit — you can post it as a reply)."),
        _pl("comment", True, "A link in a reply to your own post is clickable."),
        _pl("bio", True, "A link in the profile is clickable."),
    ]},
    "reddit": {"label": "Reddit", "placements": [
        _pl("post", True, "A link in a post is clickable (check the subreddit’s self-promo rules)."),
        _pl("comment", True, "A link in a comment is clickable."),
    ]},
    "discord": {"label": "Discord", "placements": [
        _pl("post", True, "A link in a message / announcement is clickable."),
    ]},
    "telegram": {"label": "Telegram", "placements": [
        _pl("post", True, "A link in a post is clickable."),
        _pl("bio", True, "A link in the channel description is clickable."),
    ]},
    "bluesky": {"label": "Bluesky", "placements": [
        _pl("post", True, "A link in a post is clickable."),
        _pl("bio", True, "A link in the profile is clickable."),
    ]},
    "facebook": {"label": "Facebook", "placements": [
        _pl("post", True, "A link in a post is clickable."),
        _pl("video", True, "A link in a video description is clickable (not in Reels)."),
        _pl("comment", True, "A link in a comment is clickable."),
        _pl("bio", True, "A link in the page profile is clickable."),
    ]},
    "itch": {"label": "itch.io", "placements": [
        _pl("description", True, "A link in the game page description is clickable."),
        _pl("post", True, "A link in a devlog is clickable."),
    ]},
    "other": {"label": "Other", "placements": [
        _pl("post", None, "Check yourself whether the link is clickable."),
        _pl("bio", None, "Check yourself whether the link is clickable."),
        _pl("description", None, "Check yourself whether the link is clickable."),
        _pl("video", None, "Check yourself whether the link is clickable."),
    ]},
}

# “Campaign set”: the links worth placing for any campaign
CAMPAIGN_SET: list[tuple[str, str]] = [
    ("youtube", "bio"), ("tiktok", "bio"), ("instagram", "bio"),
    ("x", "post"), ("telegram", "post"), ("discord", "post"), ("vk", "post"), ("reddit", "post"),
]

# publishing platforms (PlatformEnum values) → UTM source
PLATFORM_SOURCE = {
    "yt": "youtube", "tg": "telegram", "ig_reels": "instagram", "fb_ru": "facebook",
    "fb_en": "facebook", "vk_video": "vk",
}


def placement(source: str, medium: str) -> Optional[dict]:
    src = SOURCES.get(source)
    return next((p for p in src["placements"] if p["key"] == medium), None) if src else None


def source_label(source: str) -> str:
    return (SOURCES.get(source) or {}).get("label", source)


def medium_label(medium: str) -> str:
    return PLACEMENT_LABELS.get(medium, medium)


# ── URLs ─────────────────────────────────────────────────────────────────────
UTM_KEYS = ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content")


def steam_base_url(appid) -> str:
    return f"https://store.steampowered.com/app/{int(appid)}/"


def build_url(base_url: str, source: str, medium: str, campaign: str, content: str = "", term: str = "") -> str:
    """base_url + utm_* params. Existing params of base_url are kept (old utm_* ones
    are replaced), empty values are left out, everything is percent-encoded."""
    parts = urlsplit(base_url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k not in UTM_KEYS]
    values = (source, medium, campaign, term, content)
    query += [(k, v) for k, v in zip(UTM_KEYS, values) if v]
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def check_base_url(url: str) -> str:
    """A custom base URL (itch.io page, press kit…): http(s) with a host, or ValueError."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc or len(url) > 500 or " " in url:
        raise ValueError("the link must start with https:// and point to a page")
    return url


PLACEHOLDER = "{steam}"


def expand_placeholders(text: str, *, appid, platform: str, medium: str, campaign: str) -> str:
    """Replace every {steam} in a post text with the Steam link tagged for that platform.
    Text without the token, or a project without an app id, is returned unchanged."""
    if not text or PLACEHOLDER not in text or not appid:
        return text
    source = PLATFORM_SOURCE.get(platform, platform)
    source = source if source in SOURCES else (slug(source) or "other")
    url = build_url(steam_base_url(appid), source, slug(medium), slug(campaign))
    return text.replace(PLACEHOLDER, url)
