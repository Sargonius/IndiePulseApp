"""YouTube setup checklist (Accounts page) and the "YouTube is disconnected" alert.

Everything here is derived from settings + the posts table; the live token check
lives in the router (it refreshes the token, which records/clears yt_auth_error).
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from time import time as _now

CONSOLE = "https://console.cloud.google.com"
DAY = 86400
TESTING_TOKEN_DAYS = 7      # refresh-token lifetime Google gives apps in "Testing"
EXPIRY_WARN_DAYS = 2        # page banner this long before a reported token expiry


def project_number(client_id: str) -> str:
    """OAuth client ids look like '<project number>-<hash>.apps.googleusercontent.com'."""
    m = re.match(r"\s*(\d{5,})-", client_id or "")
    return m.group(1) if m else ""


def console_links(client_id: str) -> dict:
    """Google Cloud pages for each step, with the project preselected when the client
    id gives it away (the console accepts the project number in ?project=)."""
    num = project_number(client_id)
    q = f"?project={num}" if num else ""
    return {
        "project": num,
        "api": f"{CONSOLE}/apis/library/youtube.googleapis.com{q}",
        "clients": f"{CONSOLE}/auth/clients{q}",
        "audience": f"{CONSOLE}/auth/audience{q}",
        "quotas": f"{CONSOLE}/apis/api/youtube.googleapis.com/quotas{q}",
        "permissions": "https://myaccount.google.com/permissions",
    }


def auth_reason(error: str) -> str:
    """Human wording for a yt_refresh_failed error."""
    if "invalid_grant" in error:
        return "the refresh token expired or was revoked (invalid_grant)"
    if "invalid_client" in error:
        return "the Client ID or Client Secret is wrong (invalid_client) — check step 2"
    if "unauthorized_client" in error:
        return "this OAuth client can’t issue these tokens (unauthorized_client) — check step 2"
    return error


def fmt_when(ts) -> str:
    """Local 'Oct 4, 14:30' for a unix timestamp or a naive-UTC ISO string."""
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        loc = dt.astimezone()
        return f"{loc:%b} {loc.day}, {loc:%H:%M}"
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def metrics_summary(session) -> dict:
    """How the stats of published YouTube videos look right now."""
    from sqlmodel import select
    from app.models.models import Post, PostStatusEnum, PlatformEnum
    rows = session.exec(select(Post.metrics_json).where(
        Post.platform == PlatformEnum.yt,
        Post.status == PostStatusEnum.done,
        Post.platform_post_id.is_not(None),
    )).all()
    out = {"total": len(rows), "ok": 0, "failed": 0, "error": "", "checked_at": ""}
    last_error_at = ""
    for raw in rows:
        try:
            m = json.loads(raw or "{}") or {}
        except ValueError:
            m = {}
        checked = str(m.get("checked_at") or "")
        out["checked_at"] = max(out["checked_at"], checked)
        if m.get("error"):
            out["failed"] += 1
            if checked >= last_error_at:
                last_error_at, out["error"] = checked, str(m["error"])
        elif any(isinstance(v, int) and not isinstance(v, bool) for v in m.values()):
            out["ok"] += 1
    return out


def left_text(seconds: int) -> str:
    """'6 d 23 h' / '5 h 12 min' / '40 min'."""
    minutes = max(0, int(seconds)) // 60
    days, hours, mins = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if days:
        return f"{days} d {hours} h"
    return f"{hours} h {mins} min" if hours else f"{mins} min"


def _videos(n: int) -> str:
    return "video" if n == 1 else "videos"


def _client_problem(error: str) -> bool:
    return "invalid_client" in error or "unauthorized_client" in error


def connection(cfg: dict, now: float | None = None) -> dict:
    """Where the YouTube token stands — shared by the checklist, the page banner, the
    Accounts badge and the tray reminder.

    state: off (never connected) | error (refresh failed) | expired | expiring | ok.
    Apps in Google's "Testing" status get 7-day refresh tokens; Google reports that
    expiry at connect time (yt_refresh_expires_at), so the weekly reconnect can be
    announced ahead of time instead of discovered through failed uploads.
    """
    now = int(now or _now())
    error = str(cfg.get("yt_auth_error") or "").strip()
    expires_at = int(cfg.get("yt_refresh_expires_at") or 0)
    if not str(cfg.get("yt_refresh_token") or "").strip():
        state = "off"
    elif error:
        state = "error"
    elif expires_at and now >= expires_at:
        state = "expired"
    elif expires_at and expires_at - now < EXPIRY_WARN_DAYS * DAY:
        state = "expiring"
    else:
        state = "ok"
    return {"state": state, "error": error, "error_at": int(cfg.get("yt_auth_error_at") or 0),
            "expires_at": expires_at, "left": left_text(expires_at - now) if expires_at else "",
            "reconnect": state in ("error", "expired", "expiring", "ok") and not _client_problem(error)}


def _step(status: str, note: str) -> dict:
    return {"status": status, "note": note}


def setup_state(cfg: dict, metrics: dict, now: float | None = None) -> dict:
    """Overall verdict + per-step status for the checklist
    (done/todo/warn/error/optional/unknown)."""
    now = int(now or _now())
    s = lambda k: str(cfg.get(k) or "").strip()  # noqa: E731
    client_ok = bool(s("yt_client_id") and s("yt_client_secret"))
    connected = bool(s("yt_refresh_token"))
    conn = connection(cfg, now)
    error = conn["error"]
    error_at = conn["error_at"]
    connected_at = int(cfg.get("yt_connected_at") or 0)
    expires_at = conn["expires_at"]
    until = f"{fmt_when(expires_at)} ({conn['left']} left)" if expires_at else ""
    channel = s("yt_channel")
    last_metric_error = metrics.get("error") or ""

    # 1. YouTube Data API v3 enabled
    if "accessNotConfigured" in last_metric_error or "SERVICE_DISABLED" in last_metric_error:
        api = _step("error", "API is disabled — Google answers accessNotConfigured")
    elif metrics.get("ok") or channel:
        api = _step("done", "working")
    else:
        api = _step("unknown", "checked once you connect")

    # 2. OAuth client
    client = (_step("done", "Client ID and Client Secret saved") if client_ok
              else _step("todo", "fill in Client ID and Client Secret on the left and click Save"))

    # 3. App published (In production) — optional: staying in Testing only means a
    #    reconnect every 7 days, which IndiePulse reminds about
    lived = error_at - connected_at if (error_at and connected_at) else 0
    if expires_at or (error and 6 * DAY <= lived <= 8 * DAY):
        publish = _step("optional", "optional: the app is in Testing — the token lives 7 days, "
                                    "reconnect once a week (IndiePulse will remind you)")
    elif connected and not error and connected_at and now - connected_at > TESTING_TOKEN_DAYS * DAY:
        publish = _step("done", "verified: the token lives longer than 7 days")
    elif "invalid_grant" in error:
        publish = _step("optional", "optional: if the app is in Testing, the token lives 7 days — "
                                    "reconnect once a week")
    else:
        publish = _step("unknown", "can’t be checked automatically; in Testing just reconnect once a week")

    # 4. Channel connected, token refreshes
    who = f"channel “{channel}”" if channel else ""
    if conn["state"] == "off":
        connect = _step("todo", "not connected yet")
    elif conn["state"] == "error":
        since = f" (since {fmt_when(error_at)})" if error_at else ""
        connect = _step("error", f"{auth_reason(error)}{since}")
    elif conn["state"] == "expired":
        connect = _step("error", f"token expired on {fmt_when(expires_at)} — reconnect")
    elif conn["state"] == "expiring":
        connect = _step("warn", f"token expires in {conn['left']} ({fmt_when(expires_at)}) — reconnect")
    elif expires_at:
        connect = _step("done", f"working until {until}" + (f", {who}" if who else ""))
    else:
        parts = [p for p in (who, f"connected {fmt_when(connected_at)}" if connected_at else "") if p]
        connect = _step("done", "working" + (": " + ", ".join(parts) if parts else ""))

    # 5. Stats are flowing
    total, ok, failed = metrics.get("total", 0), metrics.get("ok", 0), metrics.get("failed", 0)
    checked = f", last checked {fmt_when(metrics['checked_at'])}" if metrics.get("checked_at") else ""
    if not total:
        stats = _step("unknown", "no published videos yet")
    elif conn["state"] in ("error", "expired"):
        stats = _step("error", f"not collected until step 4 is fixed ({ok} of {total} {_videos(total)} have stats)")
    elif failed:
        stats = _step("error", f"{failed} of {total} {_videos(total)} failed{checked}")
    elif ok == total:
        stats = _step("done", f"{total} {_videos(total)} with stats{checked}")
    else:
        stats = _step("todo", f"{ok} of {total} {_videos(total)} have stats — the rest will arrive within 30 min")

    # Verdict on top of the checklist
    state = conn["state"]
    if not (client_ok or connected):
        overall = _step("todo", "YouTube isn’t connected — go through steps 1–4 in order.")
    elif state == "error":
        fix = "check the Client ID and Secret (step 2)" if _client_problem(error) else "reconnect (step 4)"
        overall = _step("error", f"YouTube isn’t working right now: {auth_reason(error)}. "
                                 f"YouTube uploads and stats are on hold — {fix}.")
    elif state == "expired":
        overall = _step("error", f"The YouTube token expired on {fmt_when(expires_at)} — uploads and stats are on hold. "
                                 "Reconnect (step 4).")
    elif state == "expiring":
        overall = _step("warn", f"The YouTube token expires in {conn['left']} ({fmt_when(expires_at)}) — "
                                "reconnect (step 4), or uploads and stats will stop.")
    elif not connected:
        overall = _step("todo", "Only the channel is left to connect — step 4.")
    else:
        overall = _step("done", "YouTube is working" + (f": channel “{channel}”." if channel else ".")
                        + (f" Token valid until {until} — IndiePulse will remind you to reconnect "
                           f"{EXPIRY_WARN_DAYS} days ahead." if expires_at else ""))

    return {"overall": overall, "steps": {"api": api, "client": client, "publish": publish,
                                          "connect": connect, "stats": stats},
            "metrics": metrics, "last_metric_error": last_metric_error}


def alert(cfg: dict, now: float | None = None) -> dict | None:
    """What the page banner should say, or None. Settings only — no network."""
    now = int(now or _now())
    conn = connection(cfg, now)
    state, exp = conn["state"], conn["expires_at"]
    if state == "error":
        since = f" (since {fmt_when(conn['error_at'])})" if conn["error_at"] else ""
        text = (f"YouTube is disconnected{since}: {auth_reason(conn['error'])}. "
                "YouTube uploads and stats aren’t working.")
    elif state == "expired":
        text = f"The YouTube token expired on {fmt_when(exp)} — YouTube uploads and stats have stopped."
    elif state == "expiring":
        text = (f"The YouTube token expires in {conn['left']} ({fmt_when(exp)}) — reconnect, "
                "or YouTube uploads and stats will stop.")
    else:
        return None  # ok, or YouTube isn't used at all: nothing to nag about
    return {"level": "warn" if state == "expiring" else "error", "text": text, "reconnect": conn["reconnect"]}


TRAY_REMIND_BEFORE = DAY  # the tray balloon comes on the last day, the page banner earlier
_TRAY_HOW = "Right-click the IndiePulse tray icon → “YouTube: reconnect”."


def tray_reminder(cfg: dict, now: float | None = None) -> tuple[str, str] | None:
    """(key, text) of a Windows notification for the tray app, which shows each key
    once: on the last day before the token expires and when YouTube stops working.
    The key changes with every reconnect, so next week reminds again."""
    now = int(now or _now())
    conn = connection(cfg, now)
    state, exp = conn["state"], conn["expires_at"]
    if state in ("error", "expired"):
        how = _TRAY_HOW if conn["reconnect"] else "Open the YouTube settings — the Client ID / Secret is wrong."
        return (f"off:{conn['error_at'] or exp}",
                f"YouTube is disconnected — uploads and stats are on hold. {how}")
    if state == "expiring" and exp - now <= TRAY_REMIND_BEFORE:
        return (f"soon:{exp}", f"The YouTube token expires on {fmt_when(exp)} (in {conn['left']}). {_TRAY_HOW}")
    return None


def tray_menu_label(cfg: dict, now: float | None = None) -> str:
    """Tray menu entry for the weekly reconnect ('' = hide: YouTube not connected)."""
    conn = connection(cfg, now)
    if conn["state"] == "off":
        return ""
    if conn["state"] in ("error", "expired"):
        return "⚠ YouTube disconnected — reconnect"
    if conn["expires_at"]:
        return f"YouTube: reconnect (token expires {fmt_when(conn['expires_at'])})"
    return "YouTube: reconnect"
