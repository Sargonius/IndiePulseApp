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
EXPIRY_WARN_DAYS = 3        # start warning this long before a reported token expiry


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
        return "refresh token протух или отозван (invalid_grant)"
    if "invalid_client" in error:
        return "Client ID или Client Secret не подходят (invalid_client) — проверь шаг 2"
    if "unauthorized_client" in error:
        return "этот OAuth-клиент не может выдавать такие токены (unauthorized_client) — проверь шаг 2"
    return error


def fmt_when(ts) -> str:
    """Local 'dd.mm HH:MM' for a unix timestamp or a naive-UTC ISO string."""
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)
        else:
            dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.astimezone().strftime("%d.%m %H:%M")
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


def _step(status: str, note: str) -> dict:
    return {"status": status, "note": note}


def setup_state(cfg: dict, metrics: dict, now: float | None = None) -> dict:
    """Overall verdict + per-step status for the checklist (done/todo/warn/error/unknown)."""
    now = int(now or _now())
    s = lambda k: str(cfg.get(k) or "").strip()  # noqa: E731
    client_ok = bool(s("yt_client_id") and s("yt_client_secret"))
    connected = bool(s("yt_refresh_token"))
    error = s("yt_auth_error")
    error_at = int(cfg.get("yt_auth_error_at") or 0)
    connected_at = int(cfg.get("yt_connected_at") or 0)
    expires_at = int(cfg.get("yt_refresh_expires_at") or 0)
    channel = s("yt_channel")
    last_metric_error = metrics.get("error") or ""

    # 1. YouTube Data API v3 enabled
    if "accessNotConfigured" in last_metric_error or "SERVICE_DISABLED" in last_metric_error:
        api = _step("error", "API выключен — Google отвечает accessNotConfigured")
    elif metrics.get("ok") or channel:
        api = _step("done", "работает")
    else:
        api = _step("unknown", "проверится после подключения")

    # 2. OAuth client
    client = (_step("done", "Client ID и Client Secret сохранены") if client_ok
              else _step("todo", "заполни Client ID и Client Secret слева и нажми Save"))

    # 3. App published (In production), otherwise the refresh token dies after 7 days
    lived = error_at - connected_at if (error_at and connected_at) else 0
    if expires_at:
        publish = _step("error", f"Google выдал токен только до {fmt_when(expires_at)} — приложение в режиме Testing")
    elif connected and not error and connected_at and now - connected_at > TESTING_TOKEN_DAYS * DAY:
        publish = _step("done", "проверено: токен живёт дольше 7 дней")
    elif error and 6 * DAY <= lived <= 8 * DAY:
        publish = _step("error", "токен умер через 7 дней после подключения — приложение в режиме Testing")
    elif "invalid_grant" in error:
        publish = _step("warn", "скорее всего приложение в режиме Testing (там токен живёт 7 дней) — проверь")
    else:
        publish = _step("unknown", "автоматически не проверить — убедись, что стоит In production")

    # 4. Channel connected, token refreshes
    if not connected:
        connect = _step("todo", "ещё не подключено")
    elif error:
        since = f" (с {fmt_when(error_at)})" if error_at else ""
        connect = _step("error", f"{auth_reason(error)}{since}")
    else:
        parts = [p for p in (f"канал «{channel}»" if channel else "",
                             f"подключено {fmt_when(connected_at)}" if connected_at else "") if p]
        connect = _step("done", "работает" + (": " + ", ".join(parts) if parts else ""))

    # 5. Stats are flowing
    total, ok, failed = metrics.get("total", 0), metrics.get("ok", 0), metrics.get("failed", 0)
    checked = f", последняя проверка {fmt_when(metrics['checked_at'])}" if metrics.get("checked_at") else ""
    if not total:
        stats = _step("unknown", "пока нет опубликованных видео")
    elif error:
        stats = _step("error", f"не собирается, пока не починен шаг 4 ({ok} из {total} видео со статистикой)")
    elif failed:
        stats = _step("error", f"{failed} из {total} видео с ошибкой{checked}")
    elif ok == total:
        stats = _step("done", f"{total} видео со статистикой{checked}")
    else:
        stats = _step("todo", f"{ok} из {total} видео со статистикой — остальные подтянутся в течение 30 минут")

    # Verdict on top of the checklist (and in the global alert)
    if not (client_ok or connected):
        overall = _step("todo", "YouTube не подключён — пройди шаги 1–4 по порядку.")
    elif error:
        overall = _step("error", f"YouTube сейчас не работает: {auth_reason(error)}. "
                                 "Загрузки на YouTube и статистика стоят — сделай шаги 3 и 4.")
    elif expires_at and expires_at - now < EXPIRY_WARN_DAYS * DAY:
        overall = _step("warn", f"YouTube отключится {fmt_when(expires_at)}: приложение в режиме Testing. "
                                "Сделай шаг 3, потом шаг 4.")
    elif not connected:
        overall = _step("todo", "Осталось подключить канал — шаги 3 и 4.")
    else:
        overall = _step("done", "YouTube работает" + (f": канал «{channel}»." if channel else "."))

    return {"overall": overall, "steps": {"api": api, "client": client, "publish": publish,
                                          "connect": connect, "stats": stats},
            "metrics": metrics, "last_metric_error": last_metric_error}


def alert(cfg: dict, now: float | None = None) -> dict | None:
    """What the global banner should say, or None. Settings only — no network."""
    now = int(now or _now())
    if not str(cfg.get("yt_refresh_token") or "").strip():
        return None  # YouTube isn't used (or not connected yet): nothing to nag about
    error = str(cfg.get("yt_auth_error") or "").strip()
    if error:
        since = int(cfg.get("yt_auth_error_at") or 0)
        return {"level": "error",
                "text": f"YouTube отключён{' с ' + fmt_when(since) if since else ''}: {auth_reason(error)}. "
                        "Загрузки на YouTube и статистика не работают."}
    expires_at = int(cfg.get("yt_refresh_expires_at") or 0)
    if expires_at and expires_at - now < EXPIRY_WARN_DAYS * DAY:
        return {"level": "warn",
                "text": f"YouTube отключится {fmt_when(expires_at)}: Google выдал токен на 7 дней "
                        "(приложение в режиме Testing)."}
    return None
