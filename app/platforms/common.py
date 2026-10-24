"""Helpers shared by the platform publishers."""
from __future__ import annotations

import json
import re

from sqlmodel import select

from app.services import utm

DEFAULT_CAMPAIGN = "organic"
# Brief fields a post may replace with its own text (Post.overrides_json)
OVERRIDABLE = ("title", "text_ru", "text_en", "tags_json")


def _campaign(session, brief) -> str:
    """Campaign slug = the goal this publication was made for."""
    from app.models.models import Goal, GoalLink
    link = session.exec(select(GoalLink).where(GoalLink.brief_id == brief.id)
                        .order_by(GoalLink.id)).first()
    goal = session.get(Goal, link.goal_id) if link else None
    return (utm.slug(goal.title) if goal else "") or DEFAULT_CAMPAIGN


def post_overrides(post) -> dict:
    """The post's own texts ({title, text_ru, text_en, tags_json}); {} when it has none."""
    try:
        data = json.loads(getattr(post, "overrides_json", None) or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: str(v) for k, v in data.items() if k in OVERRIDABLE and v is not None}


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
    brief = session.get(Brief, post.brief_id)
    if brief is None:
        return None
    overrides = post_overrides(post)
    texts = {f: overrides.get(f, getattr(brief, f)) for f in ("text_en", "text_ru")}
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
    data.update(overrides)
    data.update(text_en=expand(texts["text_en"]), text_ru=expand(texts["text_ru"]))
    return Brief(**data)
