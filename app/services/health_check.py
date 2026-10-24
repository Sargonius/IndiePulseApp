"""API health check — verifies every configured integration WITHOUT posting anything.

Each check uses only read-only / auth-probe calls (getMe, users/me, debug_token,
createSession, webhook GET, ...). Checks that rely on refresh tokens (X, YouTube,
TikTok) do refresh them when needed and persist the new pair — exactly what a real
publish would do, and it keeps long-idle refresh tokens alive.

Result of one check:
    {id, name, status: ok|warn|error|off, summary, details: [str], hint, ms}

Plus a publish-history block per platform taken from the local DB (last success,
last error, errors since the last success) — "what broke while the app sat idle".
"""
from __future__ import annotations

import json
import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Callable, Optional

import httpx
from loguru import logger

from app.core.redact import redact
from app.services.settings import load_settings


LAST_RESULT_PATH = os.path.join("logs", "health_last.json")
FB_VERSION = "v19.0"   # same Graph version the publish code uses
IG_VERSION = "v21.0"
VK_VERSION = "5.199"
TIMEOUT = httpx.Timeout(15.0, connect=10.0)


class _Result:
    def __init__(self, cid: str, name: str) -> None:
        self.id = cid
        self.name = name
        self.status = "ok"
        self.summary = ""
        self.details: list[str] = []
        self.hint = ""

    # status only ever gets worse within one check
    _RANK = {"off": 0, "ok": 1, "warn": 2, "error": 3}

    def worsen(self, status: str) -> None:
        if self.status == "off" or self._RANK[status] > self._RANK[self.status]:
            self.status = status

    def ok(self, line: str) -> None:
        self.details.append("✓ " + line)

    def warn(self, line: str, hint: str = "") -> None:
        self.worsen("warn")
        self.details.append("⚠ " + line)
        if hint and not self.hint:
            self.hint = hint

    def err(self, line: str, hint: str = "") -> None:
        self.worsen("error")
        self.details.append("✗ " + line)
        if hint and not self.hint:
            self.hint = hint


def _s(cfg: dict, key: str) -> str:
    return str(cfg.get(key) or "").strip()


def _short(obj, n: int = 220) -> str:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return text if len(text) <= n else text[:n] + "…"


def _json(r: httpx.Response):
    try:
        return r.json()
    except Exception:
        return {"_raw": r.text[:300]}


# ── platform checks ──────────────────────────────────────────────────────────

def _more_targets(cfg: dict, r: _Result, module: str, key: str, seen: list, norm=lambda v: v) -> list[tuple[str, dict]]:
    """The destinations of `module` (Settings → Languages & destinations) whose target `key` isn't one of `seen`
    (what the old settings keys point at, checked as before): [(its name, its target)]. One that is turned off is
    noted, not checked."""
    from app.services import destinations
    out = []
    for d in destinations.all_destinations(cfg):
        val = norm(str(d["target"].get(key) or "").strip()) if d["module"] == module else ""
        if not val or val in seen:
            continue
        if not d["enabled"]:
            r.details.append(f"ℹ {destinations.title(d)}: turned off — not checked")
            continue
        seen.append(val)
        out.append((destinations.title(d), d["target"]))
    return out


def check_discord(cfg: dict, r: _Result) -> None:
    hooks = []
    for key, label in (("discord_webhook_ru", "RU"), ("discord_webhook_en", "EN"), ("discord_webhook_url", "default")):
        url = _s(cfg, key)
        if url and url not in [h[1] for h in hooks]:
            hooks.append((label, url))
    hooks += [(label, t["webhook"]) for label, t in _more_targets(cfg, r, "discord", "webhook", [h[1] for h in hooks])]
    bot = _s(cfg, "discord_bot_token")
    if not hooks and not bot:
        r.status = "off"
        r.summary = "not configured"
        return
    with httpx.Client(timeout=TIMEOUT) as c:
        # GET on a webhook URL returns its metadata and posts nothing
        for label, url in hooks:
            resp = c.get(url)
            if resp.status_code == 200:
                d = _json(resp)
                r.ok(f"webhook {label}: “{d.get('name')}” → channel {d.get('channel_id')}")
            elif resp.status_code in (401, 404):
                r.err(f"webhook {label}: deleted or wrong URL (HTTP {resp.status_code})",
                      "Create a new webhook in the Discord channel settings and paste its URL into Settings.")
            else:
                r.warn(f"webhook {label}: HTTP {resp.status_code} {_short(resp.text)}")
        if bot:
            resp = c.get("https://discord.com/api/v10/users/@me",
                         headers={"Authorization": f"Bot {bot}", "User-Agent": "IndiePulse/1.0"})
            if resp.status_code == 200:
                r.ok(f"bot token: {_json(resp).get('username')} (reactions for analytics)")
            else:
                r.err(f"bot token rejected (HTTP {resp.status_code})",
                      "Discord Developer Portal → Bot → Reset Token, then save the new one in Settings.")
        else:
            r.warn("bot token not set — reactions won’t be collected for analytics")
    r.summary = f"{len(hooks)} webhook(s)" + (" + bot" if bot else "")


def _telegram_chat(c: httpx.Client, base: str, bot_id, chat_id: str, label: str, r: _Result) -> Optional[str]:
    """One channel / chat the bot posts to → its title, None when the bot can't reach it."""
    pre = f"{label}: " if label else ""
    ch = _json(c.get(f"{base}/getChat", params={"chat_id": chat_id}))
    if not ch.get("ok"):
        r.err(f"{pre}getChat {chat_id}: {ch.get('description')}",
              "The bot was removed from the channel or the ID is wrong — add the bot as an admin again.")
        return None
    title = ch["result"].get("title") or ch["result"].get("username")
    ctype = ch["result"].get("type")
    r.ok(f"{pre}chat “{title}” ({ctype})")
    mem = _json(c.get(f"{base}/getChatMember", params={"chat_id": chat_id, "user_id": bot_id}))
    st = (mem.get("result") or {}).get("status")
    if ctype == "channel":
        if st == "administrator" and (mem["result"].get("can_post_messages") is not False):
            r.ok(f"{pre}bot is an admin with posting rights")
        elif st in ("creator",):
            r.ok(f"{pre}bot is the owner")
        else:
            r.err(f"{pre}bot is in the channel with status “{st}”, no posting rights",
                  "Make the bot a channel admin with the “Post messages” right.")
    elif st in ("left", "kicked"):
        r.err(f"{pre}bot is not a member of the chat (status={st})")
    else:
        r.ok(f"{pre}bot status: {st}")
    return title


def check_telegram(cfg: dict, r: _Result) -> None:
    token, chat_id = _s(cfg, "tg_bot_token"), _s(cfg, "tg_chat_id_ru")
    if not token:
        r.status = "off"
        r.summary = "not configured"
        return
    # the original channel, then every other channel of Settings → Languages & destinations
    chats = [("", chat_id)] if chat_id else []
    chats += [(label, t["chat_id"]) for label, t in _more_targets(cfg, r, "telegram", "chat_id", [chat_id])]
    if len(chats) > 1 and chat_id:
        chats[0] = (_linked_title(cfg, "tg_chat_id_ru") or "RU", chat_id)
    base = f"https://api.telegram.org/bot{token}"
    with httpx.Client(timeout=TIMEOUT) as c:
        me = _json(c.get(f"{base}/getMe"))
        if not me.get("ok"):
            r.err(f"getMe: {me.get('description') or _short(me)}",
                  "The bot token was revoked — get a new one from @BotFather (/token) and save it in Settings.")
            r.summary = "bot token doesn’t work"
            return
        bot_id = me["result"]["id"]
        r.ok(f"bot @{me['result'].get('username')}")
        if not chats:
            r.warn("chat id not set", "Enter the channel/chat ID in Settings.")
            r.summary = f"@{me['result'].get('username')}, no chat"
            return
        titles = [_telegram_chat(c, base, bot_id, cid, label, r) for label, cid in chats]
    reached = [t for t in titles if t]
    r.summary = ", ".join(f"“{t}”" for t in reached) if reached else "no access to the chat"


def _linked_title(cfg: dict, key: str) -> str:
    """The name of the destination that is linked to this old settings key ('' when none is)."""
    from app.services import destinations
    d = next((d for d in destinations.stored(cfg) if key in d["linked"].values()), None)
    return destinations.title(d) if d else ""


_VK_SCOPES = {"photos": 4, "video": 16, "wall": 8192, "offline": 65536}
_VK_GROUPS_SCOPE = 262144


def _vk(c: httpx.Client, method: str, token: str, **params):
    params.update({"access_token": token, "v": VK_VERSION})
    return _json(c.get(f"https://api.vk.com/method/{method}", params=params))


def _vk_token_check(c: httpx.Client, r: _Result, label: str, token: str, group_id: str) -> None:
    perms = _vk(c, "account.getAppPermissions", token)
    err = perms.get("error") or {}
    if err.get("error_code") == 5:
        r.err(f"{label}: token is invalid ({err.get('error_msg')})",
              "Get a new token via “Get Access Token” (implicit flow) in Settings → VK and paste it into both fields.")
        return
    if err.get("error_code") == 27 or "group auth" in str(err.get("error_msg", "")).lower():
        # community token
        gp = _vk(c, "groups.getTokenPermissions", token)
        if "error" in gp:
            r.err(f"{label}: {gp['error'].get('error_msg')}")
            return
        names = [p.get("name") for p in (gp.get("response") or {}).get("permissions", [])]
        r.ok(f"{label}: community token, permissions: {', '.join(names) or '—'}")
        return
    if err:
        r.err(f"{label}: [{err.get('error_code')}] {err.get('error_msg')}")
        return
    mask = int(perms.get("response") or 0)
    missing = [n for n, bit in _VK_SCOPES.items() if not mask & bit]
    if missing:
        r.err(f"{label}: user token, missing permissions: {', '.join(missing)}",
              "Reissue the token with scopes wall,groups,offline,photos,video.")
    else:
        r.ok(f"{label}: user token, has wall/photos/video/offline permissions")
    if not mask & _VK_GROUPS_SCOPE:
        r.details.append(f"ℹ {label}: no groups scope — can’t check group admin rights (not required for posting)")
    elif group_id:
        g = _vk(c, "groups.getById", token, group_id=group_id.lstrip("-"), fields="is_admin")
        resp = g.get("response")
        items = resp.get("groups") if isinstance(resp, dict) else resp
        if items:
            gi = items[0]
            if gi.get("is_admin"):
                r.ok(f"{label}: admin of group “{gi.get('name')}”")
            else:
                r.warn(f"{label}: NOT an admin of group “{gi.get('name')}” — wall posts may fail")


def check_vk(cfg: dict, r: _Result) -> None:
    tok, utok, gid = _s(cfg, "vk_access_token"), _s(cfg, "vk_user_access_token"), _s(cfg, "vk_group_id")
    if not tok and not utok:
        r.status = "off"
        r.summary = "not configured"
        return
    if not gid:
        r.warn("group id not set")
    with httpx.Client(timeout=TIMEOUT) as c:
        if tok:
            _vk_token_check(c, r, "access token" + (" (= user token)" if tok == utok else ""), tok, gid)
        if utok and utok != tok:
            _vk_token_check(c, r, "user token (photo/video upload)", utok, "")
        elif not utok:
            r.warn("user access token not set — photo/video upload won’t work")
    r.summary = f"group {gid}" if gid else "no group"


def check_x(cfg: dict, r: _Result) -> None:
    if not _s(cfg, "x_access_token") and not _s(cfg, "x_refresh_token"):
        r.status = "off"
        r.summary = "not configured"
        return
    from app.services.tokens import x_ensure_token
    try:
        token = x_ensure_token()  # refreshes + persists if expiring
    except Exception as exc:
        r.err(f"couldn’t refresh the token: {exc}", "Reconnect X with the Authorize button in Settings.")
        r.summary = "OAuth2 token won’t refresh"
        return
    from app.services import x_billing

    def no_credits() -> None:
        r.summary = "out of credits"
        r.err("X API credits ran out (402 credits depleted) — tweets aren’t being posted",
              "Top up on console.x.com → Billing and turn on Auto-recharge. "
              "What spends credits: Settings → X.")

    with httpx.Client(timeout=TIMEOUT) as c:
        resp = c.get("https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {token}"})
        if x_billing.remember(resp.status_code, resp.text, billed=False):
            no_credits()
            return
        if resp.status_code == 200:
            user = (_json(resp).get("data") or {}).get("username")
            r.ok(f"OAuth2: @{user}")
            r.summary = f"@{user}"
            # users/me answers even on an empty balance; a tweet read is billed (≤ $0.005)
            depleted, note = x_billing.probe(token)
            if depleted:
                no_credits()
                return
            if depleted is False:
                r.ok("credits available: reading the latest tweet worked")
            else:
                r.warn(f"balance: {note}")
        elif resp.status_code == 429:
            r.warn("OAuth2: rate limit on /users/me — the token is most likely fine")
        elif resp.status_code == 401:
            r.err("OAuth2 token rejected (401)", "Reconnect X with the Authorize button in Settings.")
        else:
            r.err(f"OAuth2: HTTP {resp.status_code} {_short(_json(resp))}")
    keys = [_s(cfg, k) for k in ("x_api_key", "x_api_secret", "x_oauth_token", "x_oauth_token_secret")]
    if not all(keys):
        r.warn("OAuth1 keys not set — tweets will go out without images/video")
        return
    from requests_oauthlib import OAuth1
    import requests
    try:
        resp = requests.get("https://api.twitter.com/1.1/account/verify_credentials.json",
                            auth=OAuth1(*keys), timeout=15)
        if resp.status_code == 200:
            r.ok("OAuth1 (media upload): keys are valid")
        elif resp.status_code == 401:
            r.err("OAuth1 keys rejected (401)",
                  "Regenerate the API Key/Secret and Access Token/Secret at developer.x.com and save them in Settings.")
        else:
            # free tier has no access to v1.1 read endpoints (403 / code 453) — creds can't be verified here
            r.ok(f"OAuth1: keys accepted (HTTP {resp.status_code} — endpoint not available on this plan)")
    except Exception as exc:
        r.warn(f"OAuth1: check failed: {exc}")


def check_youtube(cfg: dict, r: _Result) -> None:
    refresh, cid, csec = _s(cfg, "yt_refresh_token"), _s(cfg, "yt_client_id"), _s(cfg, "yt_client_secret")
    if not refresh and not _s(cfg, "yt_access_token"):
        r.status = "off"
        r.summary = "not configured"
        return
    if not (refresh and cid and csec):
        r.err("no refresh token or client id/secret — the access token expires in an hour",
              "Click Connect YouTube in Settings.")
        return
    from app.services.tokens import yt_access_token, YouTubeAuthError
    try:
        access = yt_access_token(force=True)  # proves the refresh token still works
    except YouTubeAuthError as exc:
        hint = "Settings → YouTube → step 4 (Connect YouTube)."
        if "invalid_grant" in str(exc):
            hint = ("The refresh token expired or was revoked. Usually the OAuth app is in Testing "
                    "(tokens live 7 days): Settings → YouTube → step 3 (Publish app), then step 4 (Connect YouTube).")
        elif "invalid_client" in str(exc) or "unauthorized_client" in str(exc):
            hint = "Client ID / Client Secret don’t match: Settings → YouTube → step 2 (OAuth client)."
        r.err(str(exc).replace("yt_refresh_failed: ", "refresh: "), hint)
        r.summary = "needs reconnecting"
        return
    r.ok("refresh token works, access token refreshed")
    with httpx.Client(timeout=TIMEOUT) as c:
        ch = _json(c.get("https://www.googleapis.com/youtube/v3/channels",
                         params={"part": "snippet", "mine": "true"},
                         headers={"Authorization": f"Bearer {access}"}))
    items = ch.get("items") or []
    if items:
        title = items[0]["snippet"]["title"]
        r.ok(f"channel “{title}”")
        r.summary = f"“{title}”"
    elif "error" in ch:
        r.err(f"channels.list: {_short(ch['error'].get('message') or ch['error'])}")
    else:
        r.warn("the account has no channel")


def _fb_debug_token(c: httpx.Client, cfg: dict, token: str) -> Optional[dict]:
    app_id, secret = _s(cfg, "fb_app_id"), _s(cfg, "fb_app_secret")
    if not (app_id and secret):
        return None
    d = _json(c.get(f"https://graph.facebook.com/{FB_VERSION}/debug_token",
                    params={"input_token": token, "access_token": f"{app_id}|{secret}"}))
    return d.get("data")


def _expiry_note(r: _Result, label: str, ts: int, what: str, expired_is_error: bool = True) -> None:
    if not ts:
        return
    left = ts - time.time()
    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    if left <= 0:
        (r.err if expired_is_error else r.warn)(f"{label}: {what} expired {when}")
    elif left < 14 * 86400:
        r.warn(f"{label}: {what} expires {when} (in {int(left // 86400)} d)")
    else:
        r.ok(f"{label}: {what} valid until {when}")


def check_facebook(cfg: dict, r: _Result) -> None:
    pages = [(lbl, _s(cfg, f"fb_{lbl.lower()}_page_id"), _s(cfg, f"fb_{lbl.lower()}_page_access_token"))
             for lbl in ("RU", "EN")]
    pages = [p for p in pages if p[1] or p[2]]
    # then every other Page of Settings → Languages & destinations
    pages += [(label, str(t.get("page_id") or "").strip(), str(t.get("page_token") or "").strip())
              for label, t in _more_targets(cfg, r, "facebook", "page_id", [p[1] for p in pages if p[1]])]
    if not pages:
        r.status = "off"
        r.summary = "not configured"
        return
    reauth = ("Settings → Facebook → Easy setup: get a new User token in Graph API Explorer, "
              "click Fetch My Pages and “Use as RU/EN” — this renews both the token and data access.")
    names = []
    with httpx.Client(timeout=TIMEOUT) as c:
        for lbl, pid, tok in pages:
            if not (pid and tok):
                r.err(f"{lbl}: missing page id or token", reauth)
                continue
            d = _json(c.get(f"https://graph.facebook.com/{FB_VERSION}/{pid}",
                            params={"fields": "id,name", "access_token": tok}))
            if "error" in d:
                e = d["error"]
                r.err(f"{lbl}: [{e.get('code')}/{e.get('error_subcode')}] {e.get('message')}", reauth)
                continue
            names.append(d.get("name"))
            r.ok(f"{lbl}: page “{d.get('name')}”")
            # reading the page's own posts needs a live token + pages_read_engagement (the metrics path)
            feed = _json(c.get(f"https://graph.facebook.com/{FB_VERSION}/{pid}/posts",
                               params={"fields": "id", "limit": 1, "access_token": tok}))
            if "error" in feed:
                e = feed["error"]
                r.err(f"{lbl}: reading page posts: [{e.get('code')}/{e.get('error_subcode')}] {e.get('message')}", reauth)
            else:
                r.ok(f"{lbl}: page token works (reading posts OK)")
            info = _fb_debug_token(c, cfg, tok)
            if info is None:
                continue
            if not info.get("is_valid"):
                r.err(f"{lbl}: debug_token says the token is invalid", reauth)
                continue
            _expiry_note(r, lbl, int(info.get("expires_at") or 0), "token")
            # Meta's 90-day "data access" window: once lapsed, re-login is needed for user-data calls
            _expiry_note(r, lbl, int(info.get("data_access_expires_at") or 0), "data access",
                         expired_is_error=False)
            missing = {"pages_manage_posts", "pages_read_engagement"} - set(info.get("scopes") or [])
            if missing:
                r.warn(f"{lbl}: token lacks permissions {', '.join(sorted(missing))}", reauth)
    if not (_s(cfg, "fb_app_id") and _s(cfg, "fb_app_secret")):
        r.details.append("ℹ App ID/Secret not set — can’t check token lifetimes")
    r.summary = ", ".join(n for n in names if n) or "pages unavailable"


def check_instagram(cfg: dict, r: _Result) -> None:
    uid, tok = _s(cfg, "ig_user_id"), _s(cfg, "ig_access_token")
    if not uid and not tok:
        r.status = "off"
        r.summary = "export mode (videos go to a folder)"
        return
    if not (uid and tok):
        r.err("missing ig_user_id or ig_access_token")
        return
    with httpx.Client(timeout=TIMEOUT) as c:
        d = _json(c.get(f"https://graph.facebook.com/{IG_VERSION}/{uid}",
                        params={"fields": "id,username", "access_token": tok}))
        if "error" in d:
            e = d["error"]
            r.err(f"[{e.get('code')}/{e.get('error_subcode')}] {e.get('message')}",
                  "Update ig_access_token (the Page token of the page linked to the IG business account).")
            return
        r.ok(f"account @{d.get('username')}")
        r.summary = f"@{d.get('username')}"
        lim = _json(c.get(f"https://graph.facebook.com/{IG_VERSION}/{uid}/content_publishing_limit",
                          params={"fields": "quota_usage,config", "access_token": tok}))
        data = (lim.get("data") or [{}])[0]
        if "quota_usage" in data:
            total = (data.get("config") or {}).get("quota_total", "?")
            r.ok(f"publishing limit: {data['quota_usage']}/{total} per 24 h")
        elif "error" in lim:
            r.warn(f"content_publishing_limit: {lim['error'].get('message')} (needs instagram_content_publish)")
        info = _fb_debug_token(c, cfg, tok)
        if info:
            _expiry_note(r, "IG", int(info.get("expires_at") or 0), "token")
            _expiry_note(r, "IG", int(info.get("data_access_expires_at") or 0), "data access",
                         expired_is_error=False)


def check_reddit(cfg: dict, r: _Result) -> None:
    creds = [_s(cfg, k) for k in ("reddit_client_id", "reddit_client_secret", "reddit_username", "reddit_password")]
    if not any(creds):
        r.status = "off"
        r.summary = "not configured"
        return
    if not all(creds):
        r.err("not all fields are filled in (client id/secret, username, password)")
        return
    from app.services.reddit_client import RedditClient
    rc = RedditClient(*creds)
    try:
        me = rc.reddit.user.me()
        r.ok(f"u/{me.name}, karma {me.link_karma + me.comment_karma}")
        r.summary = f"u/{me.name}"
    except Exception as exc:
        r.err(f"login failed: {exc}",
              "Check the password and that the app type is “script” at reddit.com/prefs/apps; password login doesn’t work with 2FA.")
        return
    names = [_s(cfg, key).removeprefix("r/") for key in ("reddit_subreddit_en", "reddit_subreddit_ru")]
    # then every other subreddit of Settings → Languages & destinations
    names += [t["subreddit"].strip().removeprefix("r/") for _label, t in
              _more_targets(cfg, r, "reddit", "subreddit", [n for n in names if n], norm=lambda v: v.removeprefix("r/"))]
    for name in names:
        if not name:
            continue
        try:
            sub = rc.reddit.subreddit(name)
            if sub.user_is_banned:
                r.err(f"r/{name}: the account is banned in this subreddit")
            else:
                r.ok(f"r/{name}: available ({sub.subscribers} subscribers)")
        except Exception as exc:
            r.err(f"r/{name}: {exc}")


def check_tiktok(cfg: dict, r: _Result) -> None:
    if not _s(cfg, "tiktok_access_token") and not _s(cfg, "tiktok_refresh_token"):
        r.status = "off"
        r.summary = "export mode (videos go to a folder)"
        return
    from app.services.tiktok_client import get_valid_access_token
    token = get_valid_access_token(cfg)  # refreshes (and persists) if expired
    with httpx.Client(timeout=TIMEOUT) as c:
        d = _json(c.get("https://open.tiktokapis.com/v2/user/info/",
                        params={"fields": "open_id,display_name"},
                        headers={"Authorization": f"Bearer {token}"}))
    code = (d.get("error") or {}).get("code")
    if code == "ok":
        name = ((d.get("data") or {}).get("user") or {}).get("display_name") or "?"
        r.ok(f"account {name}")
        r.summary = name
    else:
        r.err(f"{code}: {(d.get('error') or {}).get('message')}",
              "A TikTok refresh token lives ~1 year — reconnect with the Connect TikTok button in Settings.")
        r.summary = "needs reconnecting"


def check_bluesky(cfg: dict, r: _Result) -> None:
    handle, pw = _s(cfg, "bluesky_handle"), _s(cfg, "bluesky_app_password")
    if not handle and not pw:
        r.status = "off"
        r.summary = "not configured"
        return
    from app.services.bluesky_client import BlueskyClient
    try:
        cl = BlueskyClient(handle, pw, _s(cfg, "bluesky_service") or "https://bsky.social")
        cl.login()
        r.ok(f"logged in as @{cl.handle}")
        r.summary = f"@{cl.handle}"
    except Exception as exc:
        r.err(f"login failed: {exc}", "Create a new App Password at bsky.app → Settings → App Passwords.")


def check_openai(cfg: dict, r: _Result) -> None:
    from app.services import openai_client
    key, model = _s(cfg, "openai_api_key"), _s(cfg, "openai_model") or openai_client.DEFAULT_MODEL
    if not key:
        r.status = "off"
        r.summary = "not configured (translation via deep-translator)"
        return
    # tiny completion: the only way to catch an exhausted balance (costs ~nothing)
    resp, model = openai_client.chat_completion([{"role": "user", "content": "ping"}],
                                                max_completion_tokens=16, model=model,
                                                api_key=key, timeout=TIMEOUT)
    d = _json(resp)
    if resp.status_code == 200:
        r.ok(f"key and balance OK, model {model}")
        r.summary = model
        return
    e = d.get("error") or {}
    msg = str(e.get("message") or "")
    if e.get("code") == "insufficient_quota" or (resp.status_code == 429 and any(
            w in msg.lower() for w in ("credit", "quota", "billing"))):
        r.summary = "out of credits"
        r.err(f"balance ran out: {_short(msg, 120)}",
              "Top up at platform.openai.com → Billing. Until then, translation uses the free fallback (deep-translator).")
    elif resp.status_code == 401:
        r.err("key rejected (401)", "Create a new key at platform.openai.com/api-keys.")
    elif e.get("code") == "model_not_found":
        r.err(f"model {model} unavailable", f"Change openai_model in Settings (e.g. {openai_client.DEFAULT_MODEL}).")
    else:
        r.warn(f"HTTP {resp.status_code}: {e.get('message') or _short(d)}")


def check_claude(cfg: dict, r: _Result) -> None:
    from app.services import anthropic_client
    key, model = _s(cfg, "anthropic_api_key"), _s(cfg, "anthropic_model") or anthropic_client.DEFAULT_MODEL
    if not key:
        r.status = "off"
        r.summary = "not configured"
        return
    # a tiny request, like OpenAI's: an empty credit balance only shows on a real one
    try:
        _text, model = anthropic_client.chat([{"role": "user", "content": "ping"}], max_tokens=64, model=model,
                                             api_key=key, timeout=TIMEOUT)
    except anthropic_client.ClaudeError as exc:
        msg = str(exc)
        if "credit balance" in msg.lower():
            r.summary = "out of credits"
            r.err(f"balance ran out: {_short(msg, 120)}",
                  "Top up at platform.claude.com → Billing. Until then, translation uses machine translation.")
        elif "401" in msg:
            r.err("key rejected (401)", "Create a new key at platform.claude.com → API keys.")
        elif "404" in msg:
            r.err(f"model {model} unavailable",
                  f"Change the Claude model in Settings (e.g. {anthropic_client.DEFAULT_MODEL}).")
        else:
            r.warn(_short(msg, 200))
        return
    r.ok(f"key and balance OK, model {model}")
    r.summary = model


def check_deepl(cfg: dict, r: _Result) -> None:
    key = _s(cfg, "deepl_api_key")
    if not key:
        r.status = "off"
        r.summary = "not configured (free public translators)"
        return
    host = "https://api-free.deepl.com" if key.endswith(":fx") else "https://api.deepl.com"
    resp = httpx.get(f"{host}/v2/usage", headers={"Authorization": f"DeepL-Auth-Key {key}"}, timeout=TIMEOUT)
    if resp.status_code == 403:
        r.err("key rejected (403)", "Copy the key again from deepl.com → Account → API keys.")
        return
    if resp.status_code >= 400:
        r.warn(f"HTTP {resp.status_code}: {_short(resp.text, 160)}")
        return
    d = _json(resp)
    used, limit = int(d.get("character_count") or 0), int(d.get("character_limit") or 0)
    r.summary = f"{used:,} / {limit:,} characters" if limit else f"{used:,} characters"
    if limit and used >= limit:
        r.err("this month's characters are used up", "Wait for the next month or upgrade at deepl.com.")
    else:
        r.ok(f"key OK, {r.summary} this month")


def check_steam(cfg: dict, r: _Result) -> None:
    """Steamworks Financial API key: yesterday's wishlist report for every game."""
    key = _s(cfg, "steam_financial_api_key")
    if not key:
        r.status = "off"
        r.summary = "not configured"
        return
    from sqlmodel import Session
    from app.db.session import engine
    from app.services import steam_partner
    from app.services.marketing_collect import steam_projects
    with Session(engine) as session:
        projects = [(p.name, p.steam_app_id) for p in steam_projects(session)]
    if not projects:
        r.warn("key is set, but no project has a Steam App ID", "Projects → Edit → Steam App ID.")
        r.summary = "no App ID"
        return
    day = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    for name, appid in projects:
        try:
            resp = steam_partner.wishlist_report(key, appid, day)
        except Exception as exc:
            r.err(f"{name}: {exc}", "You need a Web API key from a group with the Financial permission; check the key’s IP whitelist.")
            continue
        if not resp:
            r.err(f"{name}: empty response — the key has no access to app {appid}")
            continue
        adds = int((resp.get("wishlist_summary") or {}).get("wishlist_adds") or 0)
        r.ok(f"{name}: wishlist adds for {day}: +{adds}")
    r.summary = f"{len(projects)} game" + ("" if len(projects) == 1 else "s")


def check_local(cfg: dict, r: _Result) -> None:
    """ffmpeg, music folder, disk space, and whether the worker is actually draining the queue."""
    ff = None
    for cand in (os.path.join("vendor", "ffmpeg", "ffmpeg.exe"), shutil.which("ffmpeg"),
                 r"C:\ffmpeg\bin\ffmpeg.exe", r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"):
        if cand and os.path.exists(cand):
            ff = cand
            break
    if ff:
        r.ok(f"ffmpeg: {ff}")
    else:
        r.err("ffmpeg not found — video rendering won’t work", "Settings/Video → Install ffmpeg, or put it in vendor/ffmpeg/.")
    from app.services.music_library import AUDIO_EXTS, folders
    for music in folders(cfg):
        if os.path.isdir(music):
            n = sum(1 for _root, _dirs, files in os.walk(music)
                    for f in files if os.path.splitext(f)[1].lower() in AUDIO_EXTS)
            r.ok(f"music folder {music}: {n} track" + ("" if n == 1 else "s"))
        else:
            r.warn(f"music folder not found: {music}", "Settings → Video & music")
    free_gb = shutil.disk_usage(os.getcwd()).free / 1024 ** 3
    (r.ok if free_gb > 5 else r.warn)(f"free disk space: {free_gb:.1f} GB")

    from sqlmodel import Session, select
    from app.db.session import engine
    from app.models.models import Post, PostStatusEnum
    now = datetime.utcnow()
    with Session(engine) as s:
        overdue = s.exec(select(Post).where(Post.status == PostStatusEnum.queued,
                                            Post.scheduled_at != None,  # noqa: E711
                                            Post.scheduled_at < now - timedelta(minutes=15))).all()
        stuck = s.exec(select(Post).where(Post.status == PostStatusEnum.running,
                                          Post.updated_at < now - timedelta(minutes=30))).all()
    if overdue:
        r.warn(f"{len(overdue)} scheduled post(s) overdue by >15 min — the worker doesn’t seem to be running",
               "Check the IndiePulse tray icon: the “Worker” line in its menu (or logs/worker.log).")
    else:
        r.ok("no overdue scheduled posts")
    if stuck:
        r.warn(f"{len(stuck)} post(s) stuck in running for >30 min (Jobs → Cleanup stuck)")
    try:
        from app.jobs.huey_app import huey
        r.ok(f"Huey queue: {huey.pending_count()} pending, {huey.scheduled_count()} scheduled")
    except Exception as exc:
        r.warn(f"Huey queue unavailable: {exc}")
    r.summary = "ffmpeg ✓" if ff else "ffmpeg ✗"


CHECKS: list[tuple[str, str, Callable[[dict, _Result], None]]] = [
    ("telegram", "Telegram", check_telegram),
    ("discord", "Discord", check_discord),
    ("vk", "VK", check_vk),
    ("x", "X (Twitter)", check_x),
    ("youtube", "YouTube", check_youtube),
    ("facebook", "Facebook", check_facebook),
    ("instagram", "Instagram Reels", check_instagram),
    ("reddit", "Reddit", check_reddit),
    ("tiktok", "TikTok", check_tiktok),
    ("bluesky", "Bluesky", check_bluesky),
    ("openai", "OpenAI", check_openai),
    ("claude", "Claude", check_claude),
    ("deepl", "DeepL", check_deepl),
    ("steam", "Steam: wishlists & sales", check_steam),
    ("local", "Local: ffmpeg / worker / disk", check_local),
]

# DB platform enum value -> check id (for publish history)
PLATFORM_TO_CHECK = {
    "tg": "telegram", "discord": "discord", "vk": "vk", "vk_video": "vk", "x": "x", "yt": "youtube",
    "fb_ru": "facebook", "fb_en": "facebook", "ig_reels": "instagram", "reddit": "reddit",
    "tiktok": "tiktok", "bluesky": "bluesky",
}


def _secrets(cfg: dict) -> list[str]:
    marks = ("token", "secret", "password", "key", "webhook")
    return [str(v) for k, v in cfg.items() if any(m in k for m in marks) and isinstance(v, str) and len(v) >= 8]


def _run_one(cid: str, name: str, fn, cfg: dict) -> dict:
    r = _Result(cid, name)
    t0 = time.perf_counter()
    try:
        fn(cfg, r)
    except httpx.TimeoutException:
        r.err("timeout — the service didn’t answer within 15 s", "Check your internet/VPN or try again later.")
    except httpx.HTTPError as exc:
        r.err(f"network error: {type(exc).__name__}: {exc}")
    except Exception as exc:
        logger.exception(f"health.{cid} crashed: {exc}")
        r.err(f"check crashed: {type(exc).__name__}: {exc}")
    if r.status == "error" and not r.summary:
        r.summary = next((d[2:] for d in r.details if d.startswith("✗")), "error")
    # redact() re-reads settings (a refresh inside the check may have rotated tokens);
    # the pre-check snapshot covers the old values
    old = _secrets(cfg)
    return {
        "id": cid, "name": name, "status": r.status,
        "summary": redact(r.summary, old),
        "details": [redact(d, old) for d in r.details],
        "hint": r.hint,
        "ms": int((time.perf_counter() - t0) * 1000),
    }


def publish_history(days: int = 365) -> dict:
    """Per check id: last successful publish, last error, and errors since that success."""
    from sqlmodel import Session, select
    from app.db.session import engine
    from app.models.models import Post, PostStatusEnum

    since = datetime.utcnow() - timedelta(days=days)
    out: dict[str, dict] = {}
    with Session(engine) as s:
        rows = s.exec(select(Post).where(Post.created_at >= since).order_by(Post.updated_at)).all()
    for p in rows:
        plat = getattr(p.platform, "value", str(p.platform))
        cid = PLATFORM_TO_CHECK.get(plat)
        if not cid:
            continue
        h = out.setdefault(cid, {"last_ok": None, "last_error": None, "last_error_text": "",
                                 "errors_since_ok": 0, "total": 0})
        h["total"] += 1
        when = p.published_at or p.updated_at
        st = getattr(p.status, "value", str(p.status))
        if st in (PostStatusEnum.done.value, PostStatusEnum.awaiting_finish.value):
            if not h["last_ok"] or when.isoformat() > h["last_ok"]:
                h["last_ok"] = when.isoformat()
                h["errors_since_ok"] = 0
        elif st == PostStatusEnum.error.value:
            h["last_error"] = (p.updated_at or when).isoformat()
            h["last_error_text"] = _short(p.error_text or "", 300)
            h["errors_since_ok"] += 1
    return out


def run_checks(only: Optional[str] = None) -> dict:
    cfg = load_settings()
    todo = [c for c in CHECKS if not only or c[0] == only]
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_run_one, cid, name, fn, cfg) for cid, name, fn in todo]
        results = [f.result() for f in futures]

    prev = load_last() or {}
    by_id = {r["id"]: r for r in (prev.get("results") or [])} if only else {}
    for res in results:
        res["checked_at"] = datetime.now().isoformat(timespec="seconds")
        by_id[res["id"]] = res
    ordered = [by_id[c[0]] for c in CHECKS if c[0] in by_id]
    try:
        history = publish_history()
    except Exception as exc:
        logger.warning(f"health.history failed: {exc}")
        history = {}
    report = {"ran_at": datetime.now().isoformat(timespec="seconds"), "results": ordered, "history": history}
    try:
        os.makedirs(os.path.dirname(LAST_RESULT_PATH), exist_ok=True)
        with open(LAST_RESULT_PATH, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
    except Exception as exc:
        logger.warning(f"health: could not save last result: {exc}")
    return report


def load_last() -> Optional[dict]:
    try:
        with open(LAST_RESULT_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
