"""Follower counts of every connected account and of each game's Steam page.

Network only, no database: a channel's fetch takes the settings and an httpx.Client
and returns ({metric: value}, info). marketing_collect.collect_audience runs the
fetches in parallel, stores the values and keeps a status per channel.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Optional

import httpx

Reading = tuple[dict, str]
GRAPH = "https://graph.facebook.com/v19.0"


class AudienceError(RuntimeError):
    pass


def _json(r: httpx.Response, what: str) -> dict:
    try:
        data = r.json()
    except ValueError:
        data = None
    if r.status_code >= 400 or not isinstance(data, dict):
        raise AudienceError(f"{what}: HTTP {r.status_code} {r.text[:150]}")
    return data


def _count(value, what: str) -> int:
    if value is None:
        raise AudienceError(f"{what}: no follower count in the response")
    return int(value)


def _s(cfg: dict, key: str) -> str:
    return str(cfg.get(key) or "").strip()


# ── fetchers ─────────────────────────────────────────────────────────────────

def discord(cfg: dict, c: httpx.Client) -> Reading:
    """Members of the server; without a configured id, the bot's only server (saved)."""
    from app.services.settings import save_settings
    headers = {"Authorization": f"Bot {_s(cfg, 'discord_bot_token')}", "User-Agent": "IndiePulse/1.0"}
    gid = _s(cfg, "discord_guild_id")
    if not gid:
        r = c.get("https://discord.com/api/v10/users/@me/guilds", headers=headers)
        if r.status_code >= 400:
            raise AudienceError(f"guilds: HTTP {r.status_code} {r.text[:150]}")
        guilds = r.json() or []
        if len(guilds) != 1:
            raise AudienceError("the bot isn’t in any server" if not guilds else
                                "the bot is in several servers — set the server ID in ⚙️ Settings → Discord")
        gid = str(guilds[0]["id"])
        save_settings({"discord_guild_id": gid})
    r = c.get(f"https://discord.com/api/v10/guilds/{gid}", params={"with_counts": "true"}, headers=headers)
    if r.status_code >= 400:
        raise AudienceError(f"guild {gid}: HTTP {r.status_code} {r.text[:150]}")
    data = r.json()
    count = _count(data.get("approximate_member_count"), "Discord")
    return {"discord_members": count}, f"“{data.get('name') or gid}”: {count}"


def youtube(cfg: dict, c: httpx.Client) -> Reading:
    from app.services.tokens import yt_access_token
    r = c.get("https://www.googleapis.com/youtube/v3/channels",
              params={"part": "statistics,snippet", "mine": "true"},
              headers={"Authorization": f"Bearer {yt_access_token()}"})
    data = r.json() if r.content else {}
    if r.status_code >= 400:
        raise AudienceError(f"channels.list: HTTP {r.status_code} {str(data.get('error') or '')[:150]}")
    items = data.get("items") or []
    if not items:
        raise AudienceError("the YouTube account has no channel")
    stats = items[0].get("statistics") or {}
    values = {"yt_views": int(stats.get("viewCount") or 0)}
    if not stats.get("hiddenSubscriberCount") and stats.get("subscriberCount") is not None:
        values["yt_subscribers"] = int(stats["subscriberCount"])
    title = (items[0].get("snippet") or {}).get("title") or "channel"
    return values, f"“{title}”: {stats.get('subscriberCount', 'hidden')} subscribers"


def vk(cfg: dict, c: httpx.Client) -> Reading:
    raw = _s(cfg, "vk_group_id").lower()
    for prefix in ("club", "public", "event"):
        raw = raw.replace(prefix, "")
    gid = "".join(ch for ch in raw if ch.isdigit())
    r = c.get("https://api.vk.com/method/groups.getById", params={
        "group_id": gid, "fields": "members_count", "v": "5.199", "access_token": _s(cfg, "vk_access_token")})
    data = _json(r, "groups.getById")
    if data.get("error"):
        err = data["error"]
        raise AudienceError(f"VK {err.get('error_code')}: {err.get('error_msg')}")
    resp = data.get("response")
    groups = resp.get("groups") if isinstance(resp, dict) else resp  # v5.199: {"groups": [...]}
    if not groups:
        raise AudienceError(f"VK: group {gid} not found")
    count = _count(groups[0].get("members_count"), "VK")
    return {"vk_members": count}, f"“{groups[0].get('name') or gid}”: {count}"


def x(cfg: dict, c: httpx.Client) -> Reading:
    from app.services.tokens import x_ensure_token
    from app.services import x_billing
    r = c.get("https://api.twitter.com/2/users/me", params={"user.fields": "public_metrics"},
              headers={"Authorization": f"Bearer {x_ensure_token()}"})
    if x_billing.remember(r.status_code, r.text, billed=False):
        raise AudienceError("X: out of API credits (402) — top up at console.x.com")
    user = _json(r, "users/me").get("data") or {}
    count = _count((user.get("public_metrics") or {}).get("followers_count"), "X")
    return {"x_followers": count}, f"@{user.get('username') or '?'}: {count}"


def _telegram_count(cfg: dict, c: httpx.Client, chat: str, metric: str) -> Reading:
    r = c.get(f"https://api.telegram.org/bot{_s(cfg, 'tg_bot_token')}/getChatMemberCount",
              params={"chat_id": chat})
    data = r.json() if r.content else {}
    if not data.get("ok"):
        raise AudienceError(f"getChatMemberCount: {data.get('description') or r.status_code}")
    count = _count(data.get("result"), "Telegram")
    return {metric: count}, f"channel {chat}: {count}"


def telegram(cfg: dict, c: httpx.Client) -> Reading:
    return _telegram_count(cfg, c, _s(cfg, "tg_chat_id_ru"), "tg_subscribers")


def _page_count(c: httpx.Client, page_id: str, token: str, metric: str) -> Reading:
    r = c.get(f"{GRAPH}/{page_id}", params={"fields": "name,followers_count,fan_count", "access_token": token})
    data = _json(r, "page")
    count = _count(data.get("followers_count", data.get("fan_count")), "Facebook")
    return {metric: count}, f"“{data.get('name') or '?'}”: {count}"


def _facebook(lang: str) -> Callable[[dict, httpx.Client], Reading]:
    def fetch(cfg: dict, c: httpx.Client) -> Reading:
        return _page_count(c, _s(cfg, f"fb_{lang}_page_id"), _s(cfg, f"fb_{lang}_page_access_token"),
                           f"fb_{lang}_followers")
    return fetch


def instagram(cfg: dict, c: httpx.Client) -> Reading:
    r = c.get(f"{GRAPH}/{_s(cfg, 'ig_user_id')}", params={
        "fields": "username,followers_count", "access_token": _s(cfg, "ig_access_token")})
    data = _json(r, "ig user")
    count = _count(data.get("followers_count"), "Instagram")
    return {"ig_followers": count}, f"@{data.get('username') or '?'}: {count}"


def bluesky(cfg: dict, c: httpx.Client) -> Reading:
    handle = _s(cfg, "bluesky_handle")
    r = c.get("https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile", params={"actor": handle})
    count = _count(_json(r, "getProfile").get("followersCount"), "Bluesky")
    return {"bsky_followers": count}, f"@{handle}: {count}"


def tiktok(cfg: dict, c: httpx.Client) -> Reading:
    """Needs the user.info.stats scope (asked for on /tiktok)."""
    from app.services.tiktok_client import get_valid_access_token
    r = c.get("https://open.tiktokapis.com/v2/user/info/", params={"fields": "display_name,follower_count"},
              headers={"Authorization": f"Bearer {get_valid_access_token(cfg)}"})
    data = _json(r, "user/info")
    if ((data.get("error") or {}).get("code") or "ok") != "ok":
        raise AudienceError(f"TikTok: {data['error'].get('code')} {data['error'].get('message') or ''}".strip())
    user = (data.get("data") or {}).get("user") or {}
    count = _count(user.get("follower_count"), "TikTok")
    return {"tiktok_followers": count}, f"“{user.get('display_name') or '?'}”: {count}"


def _tiktok_ready(cfg: dict) -> bool:
    granted = str(cfg.get("tiktok_scope") or "").replace(" ", ",").split(",")
    return bool(_s(cfg, "tiktok_refresh_token")) and "user.info.stats" in granted


def _subreddits(cfg: dict) -> list[str]:
    names = [_s(cfg, "reddit_subreddit_en"), _s(cfg, "reddit_subreddit_ru")]
    return list(dict.fromkeys(n.removeprefix("r/") for n in names if n))


def _reddit_count(cfg: dict, names: list[str], metric: str) -> Reading:
    from app.services.reddit_client import RedditClient
    rc = RedditClient(_s(cfg, "reddit_client_id"), _s(cfg, "reddit_client_secret"),
                      _s(cfg, "reddit_username"), _s(cfg, "reddit_password"))
    counts = {name: int(rc.reddit.subreddit(name).subscribers or 0) for name in names}
    return {metric: sum(counts.values())}, ", ".join(f"r/{name}: {n}" for name, n in counts.items())


def reddit(cfg: dict, c: httpx.Client) -> Reading:
    """Subscribers of the subreddits posts go to (summed when EN and RU differ)."""
    return _reddit_count(cfg, _subreddits(cfg), "reddit_subscribers")


def steam_followers(appid: int, c: httpx.Client) -> int:
    """Members of the game's Steam community group ("followers" of the store page).
    The top-level memberCount is the live list size; the one inside groupDetails lags."""
    r = c.get(f"https://steamcommunity.com/games/{int(appid)}/memberslistxml/", params={"xml": 1})
    m = (re.search(r"</groupDetails>\s*<memberCount>(\d+)</memberCount>", r.text)
         or re.search(r"<memberCount>(\d+)</memberCount>", r.text))
    if r.status_code >= 400 or not m:
        raise AudienceError(f"app {appid}: no Steam community for the game (HTTP {r.status_code})")
    return int(m.group(1))


# ── registry ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Channel:
    key: str                       # status in kv_state "collect:<key>"
    label: str
    metric: str                    # the follower count (marketing.METRICS)
    platforms: tuple[str, ...]     # post platforms that publish to this account
    ready: Callable[[dict], bool]  # connected in Settings
    fetch: Callable[[dict, httpx.Client], Reading]
    # the destinations whose posts reach it, for a destination's own channel; () = a network's own: by platform
    destinations: tuple[str, ...] = ()


def _has(*keys: str) -> Callable[[dict], bool]:
    return lambda cfg: all(_s(cfg, k) for k in keys)


CHANNELS: list[Channel] = [
    Channel("vk", "VK", "vk_members", ("vk", "vk_video"), _has("vk_access_token", "vk_group_id"), vk),
    Channel("x", "X", "x_followers", ("x",),
            lambda cfg: bool(_s(cfg, "x_access_token") or _s(cfg, "x_refresh_token")), x),
    Channel("youtube", "YouTube", "yt_subscribers", ("yt",),
            lambda cfg: bool(_s(cfg, "yt_refresh_token") or _s(cfg, "yt_access_token")), youtube),
    Channel("tg", "Telegram", "tg_subscribers", ("tg",), _has("tg_bot_token", "tg_chat_id_ru"), telegram),
    Channel("discord", "Discord", "discord_members", ("discord",), _has("discord_bot_token"), discord),
    Channel("fb_ru", "Facebook RU", "fb_ru_followers", ("fb_ru",),
            _has("fb_ru_page_id", "fb_ru_page_access_token"), _facebook("ru")),
    Channel("fb_en", "Facebook EN", "fb_en_followers", ("fb_en",),
            _has("fb_en_page_id", "fb_en_page_access_token"), _facebook("en")),
    Channel("bluesky", "Bluesky", "bsky_followers", ("bluesky",), _has("bluesky_handle"), bluesky),
    Channel("reddit", "Reddit", "reddit_subscribers", ("reddit",),
            lambda cfg: bool(_subreddits(cfg)) and _has("reddit_client_id", "reddit_client_secret",
                                                        "reddit_username", "reddit_password")(cfg), reddit),
    Channel("instagram", "Instagram", "ig_followers", ("ig_reels",), _has("ig_user_id", "ig_access_token"),
            instagram),
    Channel("tiktok", "TikTok", "tiktok_followers", ("tiktok",), _tiktok_ready, tiktok),
]
FOLLOWER_METRICS = [ch.metric for ch in CHANNELS]
STEAM_KEY = "steam_followers"
LABELS = {**{ch.key: ch.label for ch in CHANNELS}, STEAM_KEY: "Steam: followers"}

# ── a destination's own account ──────────────────────────────────────────────
# The networks' channels above count what the original keys point at (the RU Telegram channel, the RU and EN
# Facebook Pages, the subreddits), so the destinations migrated from those keys keep their history. A
# destination with another target (Settings → Languages & destinations: a JA channel, a third Page) is counted
# on its own, as "<the network's metric>:<destination id>" (marketing.metric_kind knows these).
DESTINATION_METRICS = {"telegram": "tg_subscribers", "facebook": "fb_followers", "reddit": "reddit_subscribers"}
_TARGET_FIELD = {"telegram": "chat_id", "facebook": "page_id", "reddit": "subreddit"}


def is_follower_metric(metric: str) -> bool:
    base, _, dest = str(metric).partition(":")
    return metric in FOLLOWER_METRICS or (bool(dest) and base in DESTINATION_METRICS.values())


def _destination_channel(cfg: dict, dest: dict) -> Channel:
    from app.services import destinations
    module, target = dest["module"], dest["target"]
    metric = f"{DESTINATION_METRICS[module]}:{dest['id']}"
    chat = str(target.get("chat_id") or "").strip()
    page, token = str(target.get("page_id") or "").strip(), str(target.get("page_token") or "").strip()
    sub = str(target.get("subreddit") or "").strip().removeprefix("r/")

    def fetch(cfg: dict, c: httpx.Client) -> Reading:
        if module == "telegram":
            return _telegram_count(cfg, c, chat, metric)
        if module == "facebook":
            return _page_count(c, page, token, metric)
        return _reddit_count(cfg, [sub], metric)

    if module == "telegram":
        ready = _has("tg_bot_token")
    elif module == "facebook":
        def ready(cfg: dict) -> bool:
            return bool(token)
    else:
        ready = _has("reddit_client_id", "reddit_client_secret", "reddit_username", "reddit_password")
    return Channel(f"{module}:{dest['id']}", destinations.title(dest), metric,
                   (destinations.post_platform(dest, "post"),), ready, fetch, (dest["id"],))


def destination_channels(cfg: dict) -> list[Channel]:
    """A channel per turned-on Telegram / Facebook / Reddit destination whose target isn't one the networks'
    own channels count already."""
    from app.services import destinations
    seen = {"telegram": [_s(cfg, "tg_chat_id_ru")], "facebook": [_s(cfg, "fb_ru_page_id"), _s(cfg, "fb_en_page_id")],
            "reddit": list(_subreddits(cfg))}
    out = []
    for d in destinations.all_destinations(cfg):
        if d["module"] not in DESTINATION_METRICS or not d["enabled"]:
            continue
        val = str(d["target"].get(_TARGET_FIELD[d["module"]]) or "").strip()
        val = val.removeprefix("r/") if d["module"] == "reddit" else val
        if not val or val in seen[d["module"]]:
            continue
        seen[d["module"]].append(val)
        out.append(_destination_channel(cfg, d))
    return out


def channels(cfg: Optional[dict] = None) -> list[Channel]:
    """Every account whose followers are counted: the networks' own, then each destination's own."""
    if cfg is None:
        from app.services.settings import load_settings
        cfg = load_settings()
    return list(CHANNELS) + destination_channels(cfg)


def reached(chans: list[Channel], posts) -> set[str]:
    """Keys of the channels these posts went to: a post to a destination counted on its own → that one; any
    other post → the network's channel of its platform."""
    from app.platforms import platform_key
    own = {d: ch.key for ch in chans for d in ch.destinations}
    out = set()
    for p in posts:
        did = getattr(p, "destination_id", None)
        if did in own:
            out.add(own[did])
            continue
        plat = platform_key(p.platform)
        out |= {ch.key for ch in chans if not ch.destinations and plat in ch.platforms}
    return out
