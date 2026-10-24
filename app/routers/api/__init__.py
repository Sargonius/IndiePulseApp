"""All /api endpoints, split by area. Each module has its own APIRouter; this package
combines them under the /api prefix (main.py includes `api.router`)."""
from fastapi import APIRouter

from app.routers.api import (analytics, bluesky, calendar, content, destinations, discord, facebook, jobs,
                             marketing, outreach, posts, projects, steam, system, telegram, tiktok,
                             updates, video, vk, x, youtube)
from app.routers.api.common import _e  # noqa: F401  (re-export for older imports)
from app.routers.api.x import _x_ensure_token  # noqa: F401  (re-export for older imports)

router = APIRouter(prefix="/api")
# Order = where each area's first route sat in the old single-file api.py.
for _module in (analytics, content, posts, system, telegram, discord, vk, x, tiktok, facebook,
                bluesky, youtube, steam, video, jobs, calendar, projects, marketing, outreach, destinations,
                updates):
    router.include_router(_module.router)
