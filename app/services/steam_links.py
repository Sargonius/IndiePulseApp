"""Steamworks pages worth opening from IndiePulse.

Every URL was opened by hand on 2026-09-27. "Edit Store Page" is not here: its URL
uses an internal item id (/admin/game/edit/<itemid>), not the app id — it is one
click away on the app's landing page.
"""
from __future__ import annotations

from typing import Optional


def app_links(appid) -> list[dict]:
    a = int(appid)
    return [
        {"key": "landing", "label": "Game dashboard", "hint": "Edit Store Page, builds, keys",
         "url": f"https://partner.steamgames.com/apps/landing/{a}"},
        {"key": "wishlists", "label": "Wishlists", "hint": "total, by day, by country",
         "url": f"https://partner.steampowered.com/app/wishlist/{a}/"},
        {"key": "sales", "label": "Sales and activations",
         "url": f"https://partner.steampowered.com/app/details/{a}/"},
        {"key": "traffic", "label": "Page traffic", "hint": "visits, impressions, where they came from",
         "url": f"https://partner.steamgames.com/apps/navtrafficstats/{a}"},
        {"key": "utm", "label": "UTM analytics", "hint": "clicks on links with ?utm_",
         "url": f"https://partner.steamgames.com/apps/utmtrafficstats/{a}"},
        {"key": "marketing", "label": "Marketing & visibility",
         "url": f"https://partner.steamgames.com/apps/marketing/{a}"},
        {"key": "events", "label": "Events & announcements",
         "url": f"https://steamcommunity.com/ogg/{a}/announcements/hidden/"},
        {"key": "curators", "label": "Curator Connect",
         "url": f"https://partner.steamgames.com/curators/manage/{a}"},
        {"key": "store", "label": "Store page",
         "url": f"https://store.steampowered.com/app/{a}/"},
    ]


PUBLISHER_LINKS = [
    {"label": "All games traffic — overview", "url": "https://partner.steamgames.com/pub/navstatsoverview/"},
    {"label": "All games traffic — breakdown", "url": "https://partner.steamgames.com/pub/navstats/"},
]

# where a goal's metric can be checked by hand
_METRIC_PAGE = {
    "steam_units": "sales", "steam_revenue_usd": "sales",
    "steam_visits": "traffic", "steam_impressions": "traffic",
}


def metric_link(metric: str, appid) -> Optional[dict]:
    if not appid:
        return None
    key = "wishlists" if metric.startswith("steam_wishlist") else _METRIC_PAGE.get(metric)
    return next((link for link in app_links(appid) if link["key"] == key), None) if key else None
