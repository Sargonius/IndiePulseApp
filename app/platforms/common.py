"""Helpers shared by the platform publishers."""
from __future__ import annotations

import json
import re

from sqlmodel import select

from app.services import utm

DEFAULT_CAMPAIGN = "organic"
# Brief fields a post may replace with its own text (Post.overrides_json); text_<lang> for any
# other language of Settings → Languages too (text_ja, text_de, ...)
OVERRIDABLE = ("title", "text_ru", "text_en", "tags_json")
_LANG_TEXT = re.compile(r"^text_[a-z]{2,3}(-[a-z0-9]{2,8})?$")


def _campaign(session, brief) -> str:
    """Campaign slug = the goal this publication was made for."""
    from app.models.models import Goal, GoalLink
    link = session.exec(select(GoalLink).where(GoalLink.brief_id == brief.id)
                        .order_by(GoalLink.id)).first()
    goal = session.get(Goal, link.goal_id) if link else None
    return (utm.slug(goal.title) if goal else "") or DEFAULT_CAMPAIGN


def post_overrides(post) -> dict:
    """The post's own texts ({title, text_ru, text_en, text_<lang>..., tags_json}); {} when it has none."""
    try:
        data = json.loads(getattr(post, "overrides_json", None) or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items()
            if (k in OVERRIDABLE or _LANG_TEXT.match(str(k))) and v is not None}


def post_destination(post, cfg=None):
    """The destination this post goes to, its target filled in (app.services.destinations); None for a post
    without one — it publishes by the platform's old rules (its original channel / page / webhooks)."""
    if not getattr(post, "destination_id", None):
        return None
    from app.services import destinations
    dest = destinations.get(post.destination_id, cfg)
    if dest is None:
        # the worker refuses these before publishing (tasks.py); a direct call must not fall back to the old target
        raise RuntimeError(f"destination_missing: {post.destination_id} is no longer in "
                           f"Settings → Languages & destinations")
    return dest


def post_text_lang(brief, post, legacy=("en", "ru"), cfg=None) -> tuple:
    """(text, language) this post publishes.

    With a destination: the text in its language, else the first language's, else any. Without one (posts
    queued before destinations, reposts, the Shorts factory): the platform's own order of text_ru / text_en,
    e.g. ("ru", "en") for VK — exactly what it published before.
    """
    if brief is None:
        return "", None
    dest = post_destination(post, cfg)
    if dest is None:
        for lang in legacy:
            text = getattr(brief, f"text_{lang}", None)
            if text:
                return text, lang
        return "", None
    from app.services import languages
    return languages.text_with_lang(brief, dest["lang"], cfg)


def post_text(brief, post, legacy=("en", "ru"), cfg=None) -> str:
    return post_text_lang(brief, post, legacy, cfg)[0]


def brief_for_publish(session, post):
    """The post's Brief as this platform should publish it: the post's own texts
    (a reposted short has a caption per network) and {steam} replaced by this
    platform's UTM link.

    Returns a detached copy when anything changed, so the stored brief keeps its
    texts and the token for the other platforms. Without a Steam app id the token
    is just removed.
    """
    from app.models.models import Brief, Project
    from app.platforms import platform_key
    from app.services import languages
    brief = session.get(Brief, post.brief_id)
    if brief is None:
        return None
    overrides = post_overrides(post)
    # every language's text, the post's own ones (per language) over the brief's
    texts = languages.brief_texts(brief)
    texts.update({k[len("text_"):]: v for k, v in overrides.items() if k.startswith("text_")})
    if not overrides and not any(utm.PLACEHOLDER in (t or "") for t in texts.values()):
        return brief
    project = session.get(Project, brief.project_id) if getattr(brief, "project_id", None) else None
    appid = getattr(project, "steam_app_id", None)
    medium = "shorts" if brief.vertical_916 else ("video" if brief.type == "video" else "post")

    def expand(text):
        if not text or utm.PLACEHOLDER not in text:
            return text
        if not appid:
            return re.sub(r"[ \t]*" + re.escape(utm.PLACEHOLDER), "", text).strip()
        return utm.expand_placeholders(text, appid=appid, platform=platform_key(post.platform),
                                       medium=medium, campaign=_campaign(session, brief))

    data = brief.model_dump()
    data.update({k: v for k, v in overrides.items() if not k.startswith("text_")})
    out = Brief(**data)
    languages.set_brief_texts(out, {lang: expand(text) for lang, text in texts.items()})
    return out
