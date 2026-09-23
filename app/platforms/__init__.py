"""Per-platform publishing and metrics.

Each module exposes:
  publish(session, post, job) -> Optional[str]
      None = success, the caller marks the post done and schedules metrics;
      a string ('error', 'ok', ...) = the module already set the final state.
  fetch_metrics(post) -> dict            (where the platform has readable stats)

The Huey tasks that call these stay in app/jobs/tasks.py: queued tasks are stored
in queue.db by their module path, so moving them would orphan pending jobs.
"""
from __future__ import annotations

from typing import Callable

from app.platforms import (bluesky, discord, facebook, instagram, reddit, telegram,
                           tiktok, vk, x, youtube)


def platform_key(platform) -> str:
    """'x' for PlatformEnum.x, 'PlatformEnum.x' or 'x'."""
    val = getattr(platform, "value", None) or str(platform)
    return val.split(".", 1)[1] if val.startswith("PlatformEnum.") else val


PUBLISHERS: dict[str, Callable] = {
    "discord": discord.publish,
    "x": x.publish,
    "vk": vk.publish,
    "vk_video": vk.publish,
    "yt": youtube.publish,
    "fb_ru": facebook.publish,
    "fb_en": facebook.publish,
    "bluesky": bluesky.publish,
    "reddit": reddit.publish,
    "ig_reels": instagram.publish,
    "tiktok": tiktok.publish,
    "tg": telegram.publish,
}


def _needs_id(fn: Callable) -> Callable:
    """Most platforms can only be queried once the post has a platform id."""
    def wrapper(post) -> dict:
        return fn(post) if post.platform_post_id else {}
    return wrapper


METRICS: dict[str, Callable] = {
    "x": _needs_id(x.fetch_metrics),
    "vk": _needs_id(vk.fetch_metrics),
    "vk_video": _needs_id(vk.fetch_metrics),
    "fb_ru": facebook.fetch_metrics,     # handles a missing id itself
    "fb_en": facebook.fetch_metrics,
    "reddit": _needs_id(reddit.fetch_metrics),
    "yt": _needs_id(youtube.fetch_metrics),
    "bluesky": _needs_id(bluesky.fetch_metrics),
    "tg": _needs_id(telegram.fetch_metrics),
    "discord": discord.fetch_metrics,    # can fall back to the stored message URL
}

__all__ = ["PUBLISHERS", "METRICS", "platform_key"]
