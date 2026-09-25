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

def check_discord(cfg: dict, r: _Result) -> None:
    hooks = []
    for key, label in (("discord_webhook_ru", "RU"), ("discord_webhook_en", "EN"), ("discord_webhook_url", "default")):
        url = _s(cfg, key)
        if url and url not in [h[1] for h in hooks]:
            hooks.append((label, url))
    bot = _s(cfg, "discord_bot_token")
    if not hooks and not bot:
        r.status = "off"
        r.summary = "не настроен"
        return
    with httpx.Client(timeout=TIMEOUT) as c:
        # GET on a webhook URL returns its metadata and posts nothing
        for label, url in hooks:
            resp = c.get(url)
            if resp.status_code == 200:
                d = _json(resp)
                r.ok(f"webhook {label}: «{d.get('name')}» → channel {d.get('channel_id')}")
            elif resp.status_code in (401, 404):
                r.err(f"webhook {label}: удалён или неверный URL (HTTP {resp.status_code})",
                      "Создай новый webhook в настройках канала Discord и вставь URL в Settings.")
            else:
                r.warn(f"webhook {label}: HTTP {resp.status_code} {_short(resp.text)}")
        if bot:
            resp = c.get("https://discord.com/api/v10/users/@me",
                         headers={"Authorization": f"Bot {bot}", "User-Agent": "IndiePulse/1.0"})
            if resp.status_code == 200:
                r.ok(f"bot token: {_json(resp).get('username')} (реакции для аналитики)")
            else:
                r.err(f"bot token отклонён (HTTP {resp.status_code})",
                      "Discord Developer Portal → Bot → Reset Token, затем сохрани новый в Settings.")
        else:
            r.warn("bot token не задан — реакции в аналитике собираться не будут")
    r.summary = f"{len(hooks)} webhook(s)" + (" + bot" if bot else "")


def check_telegram(cfg: dict, r: _Result) -> None:
    token, chat_id = _s(cfg, "tg_bot_token"), _s(cfg, "tg_chat_id_ru")
    if not token:
        r.status = "off"
        r.summary = "не настроен"
        return
    base = f"https://api.telegram.org/bot{token}"
    with httpx.Client(timeout=TIMEOUT) as c:
        me = _json(c.get(f"{base}/getMe"))
        if not me.get("ok"):
            r.err(f"getMe: {me.get('description') or _short(me)}",
                  "Токен бота отозван — возьми новый у @BotFather (/token) и сохрани в Settings.")
            r.summary = "токен бота не работает"
            return
        bot_id = me["result"]["id"]
        r.ok(f"бот @{me['result'].get('username')}")
        if not chat_id:
            r.warn("chat id не задан", "Укажи ID канала/чата в Settings.")
            r.summary = f"@{me['result'].get('username')}, без чата"
            return
        ch = _json(c.get(f"{base}/getChat", params={"chat_id": chat_id}))
        if not ch.get("ok"):
            r.err(f"getChat {chat_id}: {ch.get('description')}",
                  "Бота удалили из канала или ID неверный — добавь бота админом заново.")
            r.summary = "нет доступа к чату"
            return
        title = ch["result"].get("title") or ch["result"].get("username")
        ctype = ch["result"].get("type")
        r.ok(f"чат «{title}» ({ctype})")
        mem = _json(c.get(f"{base}/getChatMember", params={"chat_id": chat_id, "user_id": bot_id}))
        st = (mem.get("result") or {}).get("status")
        if ctype == "channel":
            if st == "administrator" and (mem["result"].get("can_post_messages") is not False):
                r.ok("бот — админ с правом публикации")
            elif st in ("creator",):
                r.ok("бот — владелец")
            else:
                r.err(f"бот в канале со статусом «{st}», нет права публикации",
                      "Сделай бота администратором канала с правом «Публикация сообщений».")
        elif st in ("left", "kicked"):
            r.err(f"бот не состоит в чате (status={st})")
        else:
            r.ok(f"статус бота: {st}")
    r.summary = f"«{title}»"


_VK_SCOPES = {"photos": 4, "video": 16, "wall": 8192, "offline": 65536}
_VK_GROUPS_SCOPE = 262144


def _vk(c: httpx.Client, method: str, token: str, **params):
    params.update({"access_token": token, "v": VK_VERSION})
    return _json(c.get(f"https://api.vk.com/method/{method}", params=params))


def _vk_token_check(c: httpx.Client, r: _Result, label: str, token: str, group_id: str) -> None:
    perms = _vk(c, "account.getAppPermissions", token)
    err = perms.get("error") or {}
    if err.get("error_code") == 5:
        r.err(f"{label}: токен недействителен ({err.get('error_msg')})",
              "Получи новый токен через «Get token» (implicit flow) в Settings → VK и вставь в оба поля.")
        return
    if err.get("error_code") == 27 or "group auth" in str(err.get("error_msg", "")).lower():
        # community token
        gp = _vk(c, "groups.getTokenPermissions", token)
        if "error" in gp:
            r.err(f"{label}: {gp['error'].get('error_msg')}")
            return
        names = [p.get("name") for p in (gp.get("response") or {}).get("permissions", [])]
        r.ok(f"{label}: токен сообщества, права: {', '.join(names) or '—'}")
        return
    if err:
        r.err(f"{label}: [{err.get('error_code')}] {err.get('error_msg')}")
        return
    mask = int(perms.get("response") or 0)
    missing = [n for n, bit in _VK_SCOPES.items() if not mask & bit]
    if missing:
        r.err(f"{label}: пользовательский токен, не хватает прав: {', '.join(missing)}",
              "Перевыпусти токен со scope wall,groups,offline,photos,video.")
    else:
        r.ok(f"{label}: пользовательский токен, права wall/photos/video/offline есть")
    if not mask & _VK_GROUPS_SCOPE:
        r.details.append(f"ℹ {label}: без scope groups — права админа в группе не проверить (для постинга не обязателен)")
    elif group_id:
        g = _vk(c, "groups.getById", token, group_id=group_id.lstrip("-"), fields="is_admin")
        resp = g.get("response")
        items = resp.get("groups") if isinstance(resp, dict) else resp
        if items:
            gi = items[0]
            if gi.get("is_admin"):
                r.ok(f"{label}: админ группы «{gi.get('name')}»")
            else:
                r.warn(f"{label}: НЕ админ группы «{gi.get('name')}» — постинг на стену может не пройти")


def check_vk(cfg: dict, r: _Result) -> None:
    tok, utok, gid = _s(cfg, "vk_access_token"), _s(cfg, "vk_user_access_token"), _s(cfg, "vk_group_id")
    if not tok and not utok:
        r.status = "off"
        r.summary = "не настроен"
        return
    if not gid:
        r.warn("group id не задан")
    with httpx.Client(timeout=TIMEOUT) as c:
        if tok:
            _vk_token_check(c, r, "access token" + (" (= user token)" if tok == utok else ""), tok, gid)
        if utok and utok != tok:
            _vk_token_check(c, r, "user token (загрузка фото/видео)", utok, "")
        elif not utok:
            r.warn("user access token не задан — загрузка фото/видео не сработает")
    r.summary = f"группа {gid}" if gid else "без группы"


def check_x(cfg: dict, r: _Result) -> None:
    if not _s(cfg, "x_access_token") and not _s(cfg, "x_refresh_token"):
        r.status = "off"
        r.summary = "не настроен"
        return
    from app.services.tokens import x_ensure_token
    try:
        token = x_ensure_token()  # refreshes + persists if expiring
    except Exception as exc:
        r.err(f"не удалось обновить токен: {exc}", "Переподключи X кнопкой Connect в Settings.")
        r.summary = "OAuth2 токен не обновляется"
        return
    with httpx.Client(timeout=TIMEOUT) as c:
        resp = c.get("https://api.twitter.com/2/users/me", headers={"Authorization": f"Bearer {token}"})
        if resp.status_code == 200:
            user = (_json(resp).get("data") or {}).get("username")
            r.ok(f"OAuth2: @{user}")
            r.summary = f"@{user}"
        elif resp.status_code == 429:
            r.warn("OAuth2: rate limit на /users/me (free tier ~25/сутки) — токен скорее всего жив")
        elif resp.status_code == 401:
            r.err("OAuth2 токен отклонён (401)", "Переподключи X кнопкой Connect в Settings.")
        else:
            r.err(f"OAuth2: HTTP {resp.status_code} {_short(_json(resp))}")
    keys = [_s(cfg, k) for k in ("x_api_key", "x_api_secret", "x_oauth_token", "x_oauth_token_secret")]
    if not all(keys):
        r.warn("OAuth1 ключи не заданы — твиты уйдут без картинок/видео")
        return
    from requests_oauthlib import OAuth1
    import requests
    try:
        resp = requests.get("https://api.twitter.com/1.1/account/verify_credentials.json",
                            auth=OAuth1(*keys), timeout=15)
        if resp.status_code == 200:
            r.ok("OAuth1 (загрузка медиа): ключи валидны")
        elif resp.status_code == 401:
            r.err("OAuth1 ключи отклонены (401)",
                  "Перевыпусти API Key/Secret и Access Token/Secret в developer.x.com и сохрани в Settings.")
        else:
            # free tier has no access to v1.1 read endpoints (403 / code 453) — creds can't be verified here
            r.ok(f"OAuth1: ключи приняты (HTTP {resp.status_code} — эндпоинт недоступен на этом тарифе)")
    except Exception as exc:
        r.warn(f"OAuth1: проверка не удалась: {exc}")


def check_youtube(cfg: dict, r: _Result) -> None:
    refresh, cid, csec = _s(cfg, "yt_refresh_token"), _s(cfg, "yt_client_id"), _s(cfg, "yt_client_secret")
    if not refresh and not _s(cfg, "yt_access_token"):
        r.status = "off"
        r.summary = "не настроен"
        return
    if not (refresh and cid and csec):
        r.err("нет refresh token или client id/secret — access token истечёт через час",
              "Нажми Connect YouTube в Settings.")
        return
    from app.services.tokens import yt_access_token, YouTubeAuthError
    try:
        access = yt_access_token(force=True)  # proves the refresh token still works
    except YouTubeAuthError as exc:
        hint = "Нажми Connect YouTube в Settings."
        if "invalid_grant" in str(exc):
            hint = ("Refresh token протух/отозван. Если OAuth-приложение в Google Cloud в статусе "
                    "Testing — токены живут 7 дней; переведи его в Production. Затем Connect YouTube.")
        r.err(str(exc).replace("yt_refresh_failed: ", "refresh: "), hint)
        r.summary = "нужна переавторизация"
        return
    r.ok("refresh token работает, access token обновлён")
    with httpx.Client(timeout=TIMEOUT) as c:
        ch = _json(c.get("https://www.googleapis.com/youtube/v3/channels",
                         params={"part": "snippet", "mine": "true"},
                         headers={"Authorization": f"Bearer {access}"}))
    items = ch.get("items") or []
    if items:
        title = items[0]["snippet"]["title"]
        r.ok(f"канал «{title}»")
        r.summary = f"«{title}»"
    elif "error" in ch:
        r.err(f"channels.list: {_short(ch['error'].get('message') or ch['error'])}")
    else:
        r.warn("у аккаунта нет канала")


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
        (r.err if expired_is_error else r.warn)(f"{label}: {what} истёк {when}")
    elif left < 14 * 86400:
        r.warn(f"{label}: {what} истекает {when} (через {int(left // 86400)} дн.)")
    else:
        r.ok(f"{label}: {what} до {when}")


def check_facebook(cfg: dict, r: _Result) -> None:
    pages = [(lbl, _s(cfg, f"fb_{lbl.lower()}_page_id"), _s(cfg, f"fb_{lbl.lower()}_page_access_token"))
             for lbl in ("RU", "EN")]
    pages = [p for p in pages if p[1] or p[2]]
    if not pages:
        r.status = "off"
        r.summary = "не настроен"
        return
    reauth = ("Settings → Facebook → Easy setup: получи новый User token в Graph API Explorer, "
              "нажми Fetch pages и «Use as RU/EN» — это продлит и токен, и data access.")
    names = []
    with httpx.Client(timeout=TIMEOUT) as c:
        for lbl, pid, tok in pages:
            if not (pid and tok):
                r.err(f"{lbl}: не хватает page id или токена", reauth)
                continue
            d = _json(c.get(f"https://graph.facebook.com/{FB_VERSION}/{pid}",
                            params={"fields": "id,name", "access_token": tok}))
            if "error" in d:
                e = d["error"]
                r.err(f"{lbl}: [{e.get('code')}/{e.get('error_subcode')}] {e.get('message')}", reauth)
                continue
            names.append(d.get("name"))
            r.ok(f"{lbl}: страница «{d.get('name')}»")
            # reading the page's own posts needs a live token + pages_read_engagement (the metrics path)
            feed = _json(c.get(f"https://graph.facebook.com/{FB_VERSION}/{pid}/posts",
                               params={"fields": "id", "limit": 1, "access_token": tok}))
            if "error" in feed:
                e = feed["error"]
                r.err(f"{lbl}: чтение постов страницы: [{e.get('code')}/{e.get('error_subcode')}] {e.get('message')}", reauth)
            else:
                r.ok(f"{lbl}: токен страницы рабочий (чтение постов OK)")
            info = _fb_debug_token(c, cfg, tok)
            if info is None:
                continue
            if not info.get("is_valid"):
                r.err(f"{lbl}: debug_token говорит токен невалиден", reauth)
                continue
            _expiry_note(r, lbl, int(info.get("expires_at") or 0), "токен")
            # Meta's 90-day "data access" window: once lapsed, re-login is needed for user-data calls
            _expiry_note(r, lbl, int(info.get("data_access_expires_at") or 0), "доступ к данным (data access)",
                         expired_is_error=False)
            missing = {"pages_manage_posts", "pages_read_engagement"} - set(info.get("scopes") or [])
            if missing:
                r.warn(f"{lbl}: у токена нет прав {', '.join(sorted(missing))}", reauth)
    if not (_s(cfg, "fb_app_id") and _s(cfg, "fb_app_secret")):
        r.details.append("ℹ App ID/Secret не заданы — срок жизни токенов не проверить")
    r.summary = ", ".join(n for n in names if n) or "страницы недоступны"


def check_instagram(cfg: dict, r: _Result) -> None:
    uid, tok = _s(cfg, "ig_user_id"), _s(cfg, "ig_access_token")
    if not uid and not tok:
        r.status = "off"
        r.summary = "export mode (видео кладутся в папку)"
        return
    if not (uid and tok):
        r.err("не хватает ig_user_id или ig_access_token")
        return
    with httpx.Client(timeout=TIMEOUT) as c:
        d = _json(c.get(f"https://graph.facebook.com/{IG_VERSION}/{uid}",
                        params={"fields": "id,username", "access_token": tok}))
        if "error" in d:
            e = d["error"]
            r.err(f"[{e.get('code')}/{e.get('error_subcode')}] {e.get('message')}",
                  "Обнови ig_access_token (Page token страницы, привязанной к IG business-аккаунту).")
            return
        r.ok(f"аккаунт @{d.get('username')}")
        r.summary = f"@{d.get('username')}"
        lim = _json(c.get(f"https://graph.facebook.com/{IG_VERSION}/{uid}/content_publishing_limit",
                          params={"fields": "quota_usage,config", "access_token": tok}))
        data = (lim.get("data") or [{}])[0]
        if "quota_usage" in data:
            total = (data.get("config") or {}).get("quota_total", "?")
            r.ok(f"лимит публикаций: {data['quota_usage']}/{total} за 24ч")
        elif "error" in lim:
            r.warn(f"content_publishing_limit: {lim['error'].get('message')} (нужен instagram_content_publish)")
        info = _fb_debug_token(c, cfg, tok)
        if info:
            _expiry_note(r, "IG", int(info.get("expires_at") or 0), "токен")
            _expiry_note(r, "IG", int(info.get("data_access_expires_at") or 0), "доступ к данным (data access)",
                         expired_is_error=False)


def check_reddit(cfg: dict, r: _Result) -> None:
    creds = [_s(cfg, k) for k in ("reddit_client_id", "reddit_client_secret", "reddit_username", "reddit_password")]
    if not any(creds):
        r.status = "off"
        r.summary = "не настроен"
        return
    if not all(creds):
        r.err("заполнены не все поля (client id/secret, username, password)")
        return
    from app.services.reddit_client import RedditClient
    rc = RedditClient(*creds)
    try:
        me = rc.reddit.user.me()
        r.ok(f"u/{me.name}, karma {me.link_karma + me.comment_karma}")
        r.summary = f"u/{me.name}"
    except Exception as exc:
        r.err(f"логин не прошёл: {exc}",
              "Проверь пароль и что app типа «script» в reddit.com/prefs/apps; при 2FA логин по паролю не работает.")
        return
    for key in ("reddit_subreddit_en", "reddit_subreddit_ru"):
        name = _s(cfg, key).removeprefix("r/")
        if not name:
            continue
        try:
            sub = rc.reddit.subreddit(name)
            if sub.user_is_banned:
                r.err(f"r/{name}: аккаунт забанен в сабреддите")
            else:
                r.ok(f"r/{name}: доступен ({sub.subscribers} подписчиков)")
        except Exception as exc:
            r.err(f"r/{name}: {exc}")


def check_tiktok(cfg: dict, r: _Result) -> None:
    if not _s(cfg, "tiktok_access_token") and not _s(cfg, "tiktok_refresh_token"):
        r.status = "off"
        r.summary = "export mode (видео кладутся в папку)"
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
        r.ok(f"аккаунт {name}")
        r.summary = name
    else:
        r.err(f"{code}: {(d.get('error') or {}).get('message')}",
              "Refresh token TikTok живёт ~1 год — переподключи TikTok кнопкой Connect в Settings.")
        r.summary = "нужна переавторизация"


def check_bluesky(cfg: dict, r: _Result) -> None:
    handle, pw = _s(cfg, "bluesky_handle"), _s(cfg, "bluesky_app_password")
    if not handle and not pw:
        r.status = "off"
        r.summary = "не настроен"
        return
    from app.services.bluesky_client import BlueskyClient
    try:
        cl = BlueskyClient(handle, pw, _s(cfg, "bluesky_service") or "https://bsky.social")
        cl.login()
        r.ok(f"вход выполнен как @{cl.handle}")
        r.summary = f"@{cl.handle}"
    except Exception as exc:
        r.err(f"логин не прошёл: {exc}", "Создай новый App Password в bsky.app → Settings → App Passwords.")


def check_openai(cfg: dict, r: _Result) -> None:
    key, model = _s(cfg, "openai_api_key"), _s(cfg, "openai_model") or "gpt-4o-mini"
    if not key:
        r.status = "off"
        r.summary = "не настроен (перевод через deep-translator)"
        return
    # 1-token completion: the only way to catch an exhausted balance (costs ~nothing)
    with httpx.Client(timeout=TIMEOUT) as c:
        resp = c.post("https://api.openai.com/v1/chat/completions",
                      headers={"Authorization": f"Bearer {key}"},
                      json={"model": model, "messages": [{"role": "user", "content": "ping"}],
                            "max_completion_tokens": 5})
    d = _json(resp)
    if resp.status_code == 200:
        r.ok(f"ключ и баланс в порядке, модель {model}")
        r.summary = model
        return
    e = d.get("error") or {}
    msg = str(e.get("message") or "")
    if e.get("code") == "insufficient_quota" or (resp.status_code == 429 and any(
            w in msg.lower() for w in ("credit", "quota", "billing"))):
        r.summary = "закончились кредиты"
        r.err(f"закончился баланс: {_short(msg, 120)}",
              "Пополни баланс на platform.openai.com → Billing. До тех пор перевод идёт через бесплатный fallback (deep-translator).")
    elif resp.status_code == 401:
        r.err("ключ отклонён (401)", "Создай новый ключ на platform.openai.com/api-keys.")
    elif e.get("code") == "model_not_found":
        r.err(f"модель {model} недоступна", "Поменяй openai_model в Settings (например gpt-4o-mini).")
    else:
        r.warn(f"HTTP {resp.status_code}: {e.get('message') or _short(d)}")


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
        r.err("ffmpeg не найден — рендер видео не сработает", "Settings/Video → Install ffmpeg, или положи в vendor/ffmpeg/.")
    music = _s(cfg, "music_dir")
    if music and os.path.isdir(music):
        n = sum(1 for f in os.listdir(music) if f.lower().endswith((".mp3", ".wav", ".ogg", ".m4a", ".flac")))
        r.ok(f"папка музыки: {n} треков")
    elif music:
        r.warn(f"папка музыки не найдена: {music}")
    free_gb = shutil.disk_usage(os.getcwd()).free / 1024 ** 3
    (r.ok if free_gb > 5 else r.warn)(f"свободно на диске: {free_gb:.1f} ГБ")

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
        r.warn(f"{len(overdue)} запланированных постов просрочены >15 мин — воркер, похоже, не запущен",
               "Проверь значок IndiePulse в трее: строка «Воркер» в его меню (или logs/worker.log).")
    else:
        r.ok("просроченных запланированных постов нет")
    if stuck:
        r.warn(f"{len(stuck)} постов висят в статусе running >30 мин (Jobs → Cleanup stuck)")
    try:
        from app.jobs.huey_app import huey
        r.ok(f"очередь Huey: {huey.pending_count()} в ожидании, {huey.scheduled_count()} отложено")
    except Exception as exc:
        r.warn(f"очередь Huey недоступна: {exc}")
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
    ("local", "Локально: ffmpeg / воркер / диск", check_local),
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
        r.err("таймаут — сервис не ответил за 15 с", "Проверь интернет/VPN или повтори позже.")
    except httpx.HTTPError as exc:
        r.err(f"сетевая ошибка: {type(exc).__name__}: {exc}")
    except Exception as exc:
        logger.exception(f"health.{cid} crashed: {exc}")
        r.err(f"проверка упала: {type(exc).__name__}: {exc}")
    if r.status == "error" and not r.summary:
        r.summary = next((d[2:] for d in r.details if d.startswith("✗")), "ошибка")
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
