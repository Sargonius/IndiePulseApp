"""Steamworks partner Web API: daily wishlist and sales reports.

Both methods need the key of a Steamworks "Financial API Group" (Users &
Permissions → Manage Groups → a group with Financial permissions → its Web API
key). Docs: https://partner.steamgames.com/doc/webapi/IPartnerFinancialsService

Gotchas:
* wishlist dates are GMT days, sales dates are Pacific days, both "YYYY-MM-DD";
  GetChangedDatesForPartner returns them as "YYYY/MM/DD";
* money fields come back as strings ("9.9900");
* a key without the right permission gets an empty `{"response": {}}`, not an error.
"""
from __future__ import annotations

from typing import Optional

import httpx

BASE = "https://partner.steam-api.com/IPartnerFinancialsService"
TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class SteamPartnerError(RuntimeError):
    pass


def _get(method: str, key: str, params: dict) -> dict:
    if not (key or "").strip():
        raise SteamPartnerError("steam_financial_api_key is not set (⚙️ Settings → Steam)")
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(f"{BASE}/{method}/v001/", params={"key": key.strip(), **params})
    if r.status_code in (401, 403):
        raise SteamPartnerError(
            f"{method}: access denied (HTTP {r.status_code}) — needs a Financial API group key,"
            " or the IP isn’t on the key’s allowlist")
    if r.status_code >= 400:
        raise SteamPartnerError(f"{method}: HTTP {r.status_code} {r.text[:200]}")
    try:
        data = r.json()
    except Exception:
        raise SteamPartnerError(f"{method}: response is not JSON: {r.text[:200]}")
    return data.get("response") or {}


def dash_date(day: str) -> str:
    """'2026/04/01' → '2026-04-01' (the changed-dates call uses slashes)."""
    return (day or "").strip().replace("/", "-")


def wishlist_report(key: str, appid: int, day: str) -> dict:
    """GetAppWishlistReporting for one GMT day. Returns the `response` object:
    wishlist_summary / country_summary / language_summary / app_min_date."""
    return _get("GetAppWishlistReporting", key, {"appid": int(appid), "date": dash_date(day)})


def changed_sales_dates(key: str, highwatermark: str = "0") -> tuple[list[str], Optional[str]]:
    """Dates whose sales data changed since `highwatermark`, plus the new mark.
    The mark is None when the response is empty (usually a missing permission)."""
    resp = _get("GetChangedDatesForPartner", key, {"highwatermark": highwatermark or "0"})
    dates = [dash_date(d) for d in (resp.get("dates") or []) if d]
    mark = resp.get("result_highwatermark")
    return dates, (str(mark) if mark is not None else None)


def detailed_sales(key: str, day: str, max_pages: int = 200) -> list[dict]:
    """Every sales line item of one Pacific day, following the max_id pagination."""
    items: list[dict] = []
    mark = 0
    for _ in range(max_pages):
        resp = _get("GetDetailedSales", key, {"date": dash_date(day), "highwatermark_id": mark})
        batch = resp.get("results") or []
        items.extend(batch)
        try:
            new_mark = int(resp.get("max_id") or 0)
        except (TypeError, ValueError):
            new_mark = 0
        if not batch or new_mark <= mark:
            break
        mark = new_mark
    return items


def to_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
