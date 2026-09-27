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


def left_text(seconds: int) -> str:
    """'6 дн. 23 ч' / '5 ч 12 мин' / '40 мин'."""
    minutes = max(0, int(seconds)) // 60
    days, hours, mins = minutes // 1440, minutes % 1440 // 60, minutes % 60
    if days:
        return f"{days} дн. {hours} ч"
    return f"{hours} ч {mins} мин" if hours else f"{mins} мин"


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
    until = f"{fmt_when(expires_at)} (осталось {conn['left']})" if expires_at else ""
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

    # 3. App published (In production) — optional: staying in Testing only means a
    #    reconnect every 7 days, which IndiePulse reminds about
    lived = error_at - connected_at if (error_at and connected_at) else 0
    if expires_at or (error and 6 * DAY <= lived <= 8 * DAY):
        publish = _step("optional", "не обязательно: приложение в Testing — токен живёт 7 дней, "
                                    "переподключай раз в неделю (IndiePulse напомнит)")
    elif connected and not error and connected_at and now - connected_at > TESTING_TOKEN_DAYS * DAY:
        publish = _step("done", "проверено: токен живёт дольше 7 дней")
    elif "invalid_grant" in error:
        publish = _step("optional", "не обязательно: если приложение в Testing, токен живёт 7 дней — "
                                    "переподключай раз в неделю")
    else:
        publish = _step("unknown", "автоматически не проверить; в Testing просто переподключай раз в неделю")

    # 4. Channel connected, token refreshes
    who = f"канал «{channel}»" if channel else ""
    if conn["state"] == "off":
        connect = _step("todo", "ещё не подключено")
    elif conn["state"] == "error":
        since = f" (с {fmt_when(error_at)})" if error_at else ""
        connect = _step("error", f"{auth_reason(error)}{since}")
    elif conn["state"] == "expired":
        connect = _step("error", f"токен истёк {fmt_when(expires_at)} — переподключи")
    elif conn["state"] == "expiring":
        connect = _step("warn", f"токен истекает через {conn['left']} ({fmt_when(expires_at)}) — переподключи")
    elif expires_at:
        connect = _step("done", f"работает до {until}" + (f", {who}" if who else ""))
    else:
        parts = [p for p in (who, f"подключено {fmt_when(connected_at)}" if connected_at else "") if p]
        connect = _step("done", "работает" + (": " + ", ".join(parts) if parts else ""))

    # 5. Stats are flowing
    total, ok, failed = metrics.get("total", 0), metrics.get("ok", 0), metrics.get("failed", 0)
    checked = f", последняя проверка {fmt_when(metrics['checked_at'])}" if metrics.get("checked_at") else ""
    if not total:
        stats = _step("unknown", "пока нет опубликованных видео")
    elif conn["state"] in ("error", "expired"):
        stats = _step("error", f"не собирается, пока не починен шаг 4 ({ok} из {total} видео со статистикой)")
    elif failed:
        stats = _step("error", f"{failed} из {total} видео с ошибкой{checked}")
    elif ok == total:
        stats = _step("done", f"{total} видео со статистикой{checked}")
    else:
        stats = _step("todo", f"{ok} из {total} видео со статистикой — остальные подтянутся в течение 30 минут")

    # Verdict on top of the checklist
    state = conn["state"]
    if not (client_ok or connected):
        overall = _step("todo", "YouTube не подключён — пройди шаги 1–4 по порядку.")
    elif state == "error":
        fix = "проверь Client ID и Secret (шаг 2)" if _client_problem(error) else "переподключи (шаг 4)"
        overall = _step("error", f"YouTube сейчас не работает: {auth_reason(error)}. "
                                 f"Загрузки на YouTube и статистика стоят — {fix}.")
    elif state == "expired":
        overall = _step("error", f"Токен YouTube истёк {fmt_when(expires_at)} — загрузки и статистика стоят. "
                                 "Переподключи (шаг 4).")
    elif state == "expiring":
        overall = _step("warn", f"Токен YouTube истекает через {conn['left']} ({fmt_when(expires_at)}) — "
                                "переподключи (шаг 4), иначе загрузки и статистика остановятся.")
    elif not connected:
        overall = _step("todo", "Осталось подключить канал — шаг 4.")
    else:
        overall = _step("done", "YouTube работает" + (f": канал «{channel}»." if channel else ".")
                        + (f" Токен до {until} — за {EXPIRY_WARN_DAYS} дня IndiePulse напомнит переподключиться."
                           if expires_at else ""))

    return {"overall": overall, "steps": {"api": api, "client": client, "publish": publish,
                                          "connect": connect, "stats": stats},
            "metrics": metrics, "last_metric_error": last_metric_error}


def alert(cfg: dict, now: float | None = None) -> dict | None:
    """What the page banner should say, or None. Settings only — no network."""
    now = int(now or _now())
    conn = connection(cfg, now)
    state, exp = conn["state"], conn["expires_at"]
    if state == "error":
        since = f" с {fmt_when(conn['error_at'])}" if conn["error_at"] else ""
        text = (f"YouTube отключён{since}: {auth_reason(conn['error'])}. "
                "Загрузки на YouTube и статистика не работают.")
    elif state == "expired":
        text = f"Токен YouTube истёк {fmt_when(exp)} — загрузки на YouTube и статистика остановились."
    elif state == "expiring":
        text = (f"Токен YouTube истекает через {conn['left']} ({fmt_when(exp)}) — переподключи, "
                "иначе загрузки на YouTube и статистика остановятся.")
    else:
        return None  # ok, or YouTube isn't used at all: nothing to nag about
    return {"level": "warn" if state == "expiring" else "error", "text": text, "reconnect": conn["reconnect"]}


TRAY_REMIND_BEFORE = DAY  # the tray balloon comes on the last day, the page banner earlier
_TRAY_HOW = "Правый клик по значку IndiePulse → «YouTube: переподключить»."


def tray_reminder(cfg: dict, now: float | None = None) -> tuple[str, str] | None:
    """(key, text) of a Windows notification for the tray app, which shows each key
    once: on the last day before the token expires and when YouTube stops working.
    The key changes with every reconnect, so next week reminds again."""
    now = int(now or _now())
    conn = connection(cfg, now)
    state, exp = conn["state"], conn["expires_at"]
    if state in ("error", "expired"):
        how = _TRAY_HOW if conn["reconnect"] else "Открой настройки YouTube — Client ID / Secret не подходят."
        return (f"off:{conn['error_at'] or exp}",
                f"YouTube отключён — загрузки и статистика стоят. {how}")
    if state == "expiring" and exp - now <= TRAY_REMIND_BEFORE:
        return (f"soon:{exp}", f"Токен YouTube истекает {fmt_when(exp)} (через {conn['left']}). {_TRAY_HOW}")
    return None


def tray_menu_label(cfg: dict, now: float | None = None) -> str:
    """Tray menu entry for the weekly reconnect ('' = hide: YouTube not connected)."""
    conn = connection(cfg, now)
    if conn["state"] == "off":
        return ""
    if conn["state"] in ("error", "expired"):
        return "⚠ YouTube отключён — переподключить"
    if conn["expires_at"]:
        return f"YouTube: переподключить (токен до {fmt_when(conn['expires_at'])})"
    return "YouTube: переподключить"
