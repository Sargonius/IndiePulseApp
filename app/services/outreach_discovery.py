"""Network side of creator outreach: Steam similar games, YouTube search, RSS, link scanning.

Long runs (suggest / YouTube search / link scan) go on one daemon thread in the web
process — the Huey worker has a single slot that publishing needs. The page polls
job_status().

Limits that shape this code (verified Oct 2026):
* YouTube search.list has its own bucket: ~100 calls/day, reset at midnight Pacific.
  channels.list / videos.list cost 1 unit per 50 ids from the shared 10k pool.
* YouTube API data kept without the creator's consent must be refreshed or deleted
  within 30 days → refresh_stale() runs from the worker every 6 hours.
* Steam store data comes from IStoreBrowseService/GetItems (no key, 50 apps per call).
* No Twitch API: its Developer Agreement bans using API data for marketing outreach.
"""
from __future__ import annotations

import html
import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

import httpx
from loguru import logger
from sqlmodel import Session, select

from app.db.session import engine
from app.models.models import Creator, CreatorVideo, KVState, OutreachContact, OutreachGame, Project, now_ts
from app.services import outreach as core
from app.services.settings import load_settings as load_app_settings

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) IndiePulse/1.3 outreach",
      "Accept-Language": "en-US,en;q=0.8"}
STEAM_COOKIES = {"birthtime": "568022401", "lastagecheckage": "1-0-1988", "mature_content": "1",
                 "wants_mature_content": "1"}
YT_API = "https://www.googleapis.com/youtube/v3/"
SEARCH_DAILY_LIMIT = 100
STALE_DAYS = 25  # refresh before YouTube's 30-day limit on stored API data


# ── Steam ───────────────────────────────────────────────────────────────────
def steam_client() -> httpx.Client:
    return httpx.Client(timeout=20, follow_redirects=True, headers=UA, cookies=STEAM_COOKIES)


def similar_appids(client: httpx.Client, appid: int) -> list[tuple[int, str]]:
    """Steam's "More like this" page (HTML only): [(appid, section)] in page order.
    Sections: Similar, Upcoming Releases, Free Games, Free Demo, New Releases, Top Sellers."""
    r = client.get(f"https://store.steampowered.com/recommended/morelike/app/{int(appid)}/")
    r.raise_for_status()
    marks = [(m.start(), "h", re.sub(r"<[^>]+>", "", m.group(1)).strip())
             for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", r.text, re.S)]
    marks += [(m.start(), "a", m.group(1)) for m in re.finditer(r'data-ds-appid="(\d+)"', r.text)]
    section, out, seen = "Similar", [], {int(appid)}
    for _, kind, value in sorted(marks):
        if kind == "h":
            section = "Similar" if value.lower().startswith("looking for similar") else value
            continue
        a = int(value)
        if a not in seen:
            seen.add(a)
            out.append((a, section))
    return out


STORE_ITEMS_URL = "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/"
_tag_names: dict[int, str] = {}


def tag_names(client: httpx.Client) -> dict[int, str]:
    """Steam tag id → English name (IStoreService/GetTagList, no key needed)."""
    if not _tag_names:
        r = client.get("https://api.steampowered.com/IStoreService/GetTagList/v1/", params={"language": "english"})
        r.raise_for_status()
        _tag_names.update({t["tagid"]: t["name"] for t in (r.json().get("response") or {}).get("tags") or []})
    return _tag_names


_ITEM_TYPES = {0: "game", 1: "demo"}


def store_items(client: httpx.Client, appids) -> dict[int, dict]:
    """Store data for many apps per request (IStoreBrowseService/GetItems, no key):
    {appid: {name, type, parent, tags {name: weight}, reviews, positive}}."""
    names = tag_names(client)
    out: dict[int, dict] = {}
    for chunk in _chunks(list(dict.fromkeys(int(a) for a in appids))):
        inp = {"ids": [{"appid": a} for a in chunk], "context": {"language": "english", "country_code": "US"},
               "data_request": {"include_tag_count": 20, "include_basic_info": True, "include_reviews": True}}
        r = client.get(STORE_ITEMS_URL, params={"input_json": json.dumps(inp)})
        r.raise_for_status()
        for it in (r.json().get("response") or {}).get("store_items") or []:
            if not it.get("appid") or not it.get("name"):
                continue
            rev = (it.get("reviews") or {}).get("summary_filtered") or {}
            out[int(it["appid"])] = {
                "name": it["name"], "type": _ITEM_TYPES.get(it.get("type"), str(it.get("type"))),
                "parent": (it.get("related_items") or {}).get("parent_appid"),
                "tags": {names.get(t["tagid"], str(t["tagid"])): t.get("weight", 0) for t in it.get("tags") or []},
                "reviews": rev.get("review_count"), "positive": rev.get("percent_positive"),
            }
    return out


def steam_appdetails(client: httpx.Client, appid: int) -> dict:
    r = client.get("https://store.steampowered.com/api/appdetails",
                   params={"appids": int(appid), "l": "english", "cc": "us"})
    r.raise_for_status()
    item = (r.json() or {}).get(str(int(appid))) or {}
    return item.get("data") or {} if item.get("success") else {}


_blurbs: dict[int, str] = {}


def store_blurb(appid: int) -> str:
    """The store page's short description: the letter's fallback pitch."""
    if appid not in _blurbs:
        try:
            with steam_client() as client:
                text = steam_appdetails(client, appid).get("short_description") or ""
        except Exception:
            return ""
        _blurbs[appid] = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
    return _blurbs[appid]


def steam_search(client: httpx.Client, term: str) -> list[dict]:
    r = client.get("https://store.steampowered.com/api/storesearch/",
                   params={"term": term, "l": "english", "cc": "US"})
    r.raise_for_status()
    return [{"id": int(i["id"]), "name": i.get("name") or ""} for i in (r.json() or {}).get("items") or []]


def tag_similarity(a: dict, b: dict, top: int = 20) -> float:
    """Cosine similarity of two games' top Steam tags, weighted by vote share."""
    def vec(tags: dict) -> dict:
        items = sorted(((k, float(v)) for k, v in tags.items() if v), key=lambda kv: -kv[1])[:top]
        peak = items[0][1] if items else 1.0
        return {k: v / peak for k, v in items}
    va, vb = vec(a), vec(b)
    if not va or not vb:
        return 0.0
    dot = sum(v * vb.get(k, 0.0) for k, v in va.items())
    na = math.sqrt(sum(v * v for v in va.values()))
    nb = math.sqrt(sum(v * v for v in vb.values()))
    return round(dot / (na * nb), 3) if na and nb else 0.0


def parse_game_input(text: str) -> tuple[Optional[int], str]:
    """A Steam store link or an app id → (appid, ""); anything else → (None, name)."""
    s = (text or "").strip()
    m = re.search(r"store\.steampowered\.com/app/(\d+)", s) or re.fullmatch(r"(\d{2,9})", s)
    return (int(m.group(1)), "") if m else (None, s)


def add_game(session: Session, project_id: int, text: str) -> OutreachGame:
    """Add a similar game by Steam link, app id or name (the name is looked up in Steam
    search; with no hit the game is added by name only and searched on YouTube as is)."""
    appid, name = parse_game_input(text)
    if not appid and not name:
        raise ValueError("paste a Steam link, an app id or a game name")
    item: dict = {}
    with steam_client() as client:
        if not appid:
            hits = steam_search(client, name)
            exact = next((h for h in hits if core.norm_name(h["name"]) == core.norm_name(name)), None)
            if exact or hits:
                appid, name = (exact or hits[0])["id"], (exact or hits[0])["name"]
        if appid:
            item = store_items(client, [appid]).get(appid) or {}
            if item.get("type") == "demo" and item.get("parent"):
                appid = int(item["parent"])
                item = store_items(client, [appid]).get(appid) or {}
            name = item.get("name") or name or f"App {appid}"
        own = _project_tags(session, project_id, client)
    q = select(OutreachGame).where(OutreachGame.project_id == project_id)
    existing = session.exec(q.where(OutreachGame.steam_app_id == appid)).first() if appid else None
    existing = existing or next((g for g in session.exec(q).all()
                                 if core.norm_name(g.name) == core.norm_name(name)), None)
    if existing:
        existing.status = "active"
        session.add(existing)
        session.commit()
        return existing
    tags = item.get("tags") or {}
    game = OutreachGame(project_id=project_id, steam_app_id=appid, name=name, source="manual", status="active",
                        similarity=tag_similarity(own, tags) if own and tags else None,
                        reviews=item.get("reviews"), positive=item.get("positive"),
                        tags_json=json.dumps(list(tags)[:8]) if tags else None)
    session.add(game)
    session.commit()
    return game


_tag_cache: dict[int, dict] = {}


def _project_tags(session: Session, project_id: int, client: httpx.Client) -> dict:
    """The project's tag profile: its own Steam tags, or (no store page yet) the tags
    of the similar games marked active, each counted once per game."""
    project = session.get(Project, project_id)
    if project and project.steam_app_id:
        if project.steam_app_id not in _tag_cache:
            try:
                item = store_items(client, [project.steam_app_id]).get(project.steam_app_id) or {}
            except Exception:
                return {}
            _tag_cache[project.steam_app_id] = item.get("tags") or {}
        return _tag_cache[project.steam_app_id]
    profile: dict = {}
    for g in session.exec(select(OutreachGame).where(OutreachGame.project_id == project_id,
                                                     OutreachGame.status == "active")).all():
        for t in json.loads(g.tags_json or "[]"):
            profile[t] = profile.get(t, 0) + 1
    return profile


# ── YouTube ─────────────────────────────────────────────────────────────────
class YTError(RuntimeError):
    def __init__(self, msg: str, quota: bool = False, auth: bool = False) -> None:
        super().__init__(msg)
        self.quota, self.auth = quota, auth


def _yt_auth() -> tuple[dict, dict]:
    """An API key (never expires) if set, else the connected channel's OAuth token."""
    key = (load_app_settings().get("yt_api_key") or "").strip()
    if key:
        return {"key": key}, {}
    from app.services.tokens import YouTubeAuthError, yt_access_token
    try:
        return {}, {"Authorization": f"Bearer {yt_access_token()}"}
    except YouTubeAuthError as exc:
        raise YTError("YouTube isn’t connected (or the weekly token expired) — reconnect it in Accounts "
                      "or add a YouTube API key on this page.", auth=True) from exc


def yt_get(client: httpx.Client, method: str, params: dict) -> dict:
    qp, headers = _yt_auth()
    r = client.get(YT_API + method, params={**params, **qp}, headers=headers)
    if r.status_code == 200:
        return r.json()
    try:
        err = r.json().get("error") or {}
    except ValueError:
        err = {}
    reasons = {e.get("reason") for e in err.get("errors") or []}
    msg = (err.get("message") or f"HTTP {r.status_code}")[:200]
    if reasons & {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
        raise YTError(f"YouTube quota is used up for today ({method}) — it resets at midnight Pacific time.",
                      quota=True)
    if r.status_code in (401, 403) and reasons & {"authError", "forbidden", "keyInvalid", "accessNotConfigured"}:
        raise YTError(f"YouTube refused the request: {msg}", auth=True)
    raise YTError(f"YouTube {method}: {msg}")


def pacific_day(now: Optional[datetime] = None) -> str:
    """The day YouTube's quota counts in (US Pacific, DST by the US rule; no tzdata on Windows)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    y = now.year
    march = date(y, 3, 8)
    dst_start = datetime.combine(march + timedelta(days=(6 - march.weekday()) % 7), datetime.min.time()) + timedelta(hours=10)
    nov = date(y, 11, 1)
    dst_end = datetime.combine(nov + timedelta(days=(6 - nov.weekday()) % 7), datetime.min.time()) + timedelta(hours=9)
    offset = 7 if dst_start <= now < dst_end else 8
    return (now - timedelta(hours=offset)).strftime("%Y-%m-%d")


def _search_key() -> str:
    return f"outreach:yt_search:{pacific_day()}"


def search_calls_today(session: Session) -> int:
    row = session.get(KVState, _search_key())
    return int(row.value) if row and row.value.isdigit() else 0


def _bump_search_calls(session: Session) -> None:
    row = session.get(KVState, _search_key()) or KVState(key=_search_key(), value="0")
    row.value = str(int(row.value or 0) + 1)
    row.updated_at = now_ts()
    session.add(row)
    session.commit()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_time(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def search_videos(session: Session, client: httpx.Client, query: str, pages: int,
                  published_after: Optional[datetime]) -> list[dict]:
    """search.list pages for one query; every page counts against the daily limit."""
    out, token = [], None
    for _ in range(pages):
        if search_calls_today(session) >= SEARCH_DAILY_LIMIT:
            break
        params = {"part": "snippet", "type": "video", "q": query, "maxResults": 50, "order": "relevance"}
        if published_after:
            params["publishedAfter"] = _iso(published_after)
        if token:
            params["pageToken"] = token
        data = yt_get(client, "search", params)
        _bump_search_calls(session)
        for it in data.get("items") or []:
            sn = it.get("snippet") or {}
            vid = (it.get("id") or {}).get("videoId")
            if vid and sn.get("channelId"):
                out.append({"video_id": vid, "channel_id": sn["channelId"],
                            "title": html.unescape(sn.get("title") or ""),
                            "description": html.unescape(sn.get("description") or ""),
                            "published_at": _parse_time(sn.get("publishedAt"))})
        token = data.get("nextPageToken")
        if not token:
            break
    return out


def _chunks(seq: list, n: int = 50):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_channels(client: httpx.Client, ids: list[str]) -> dict[str, dict]:
    out = {}
    for chunk in _chunks(list(dict.fromkeys(ids))):
        data = yt_get(client, "channels", {"part": "snippet,statistics", "id": ",".join(chunk), "maxResults": 50})
        for it in data.get("items") or []:
            out[it["id"]] = it
    return out


def fetch_videos(client: httpx.Client, ids: list[str]) -> dict[str, dict]:
    out = {}
    for chunk in _chunks(list(dict.fromkeys(ids))):
        data = yt_get(client, "videos", {"part": "snippet,statistics", "id": ",".join(chunk), "maxResults": 50})
        for it in data.get("items") or []:
            out[it["id"]] = it
    return out


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def apply_channel(session: Session, item: dict) -> Creator:
    """Create/refresh a creator from a channels.list item (emails/links from the description)."""
    sn, st = item.get("snippet") or {}, item.get("statistics") or {}
    handle = sn.get("customUrl") or None
    if handle and not handle.startswith("@"):
        handle = "@" + handle
    desc = sn.get("description") or ""
    creator = core.upsert_creator(
        session, "youtube", item["id"], name=sn.get("title") or item["id"], handle=handle,
        url=f"https://www.youtube.com/{handle}" if handle else f"https://www.youtube.com/channel/{item['id']}",
        avatar_url=((sn.get("thumbnails") or {}).get("default") or {}).get("url"),
        subscribers=None if st.get("hiddenSubscriberCount") else _int(st.get("subscriberCount")),
        views=_int(st.get("viewCount")), videos=_int(st.get("videoCount")),
        country=sn.get("country"), description=desc[:5000], stats_at=now_ts())
    if sn.get("defaultLanguage") and not creator.language:
        creator.language = sn["defaultLanguage"].split("-")[0].lower()
    core.add_emails(creator, core.extract_emails(desc), "channel")
    core.add_links(creator, core.extract_links(desc))
    own = (load_app_settings().get("yt_channel") or "").lower()
    if handle and handle.lower() in own:
        creator.ignored = True  # our own channel
    session.add(creator)
    return creator


def rss_last_upload(client: httpx.Client, channel_id: str) -> Optional[datetime]:
    """The channel's newest upload from its public RSS feed (free, no API quota)."""
    try:
        r = client.get("https://www.youtube.com/feeds/videos.xml", params={"channel_id": channel_id}, timeout=10)
    except httpx.HTTPError:
        return None
    if r.status_code != 200:
        return None
    m = re.search(r"<entry>.*?<published>([^<]+)</published>", r.text, re.S)
    return _parse_time(m.group(1)) if m else None


def resolve_youtube_channel(client: httpx.Client, session: Session, parsed: dict) -> Optional[dict]:
    """A pasted channel link → its channels.list item."""
    kind, value = parsed["kind"], parsed["value"]
    params = {"part": "snippet,statistics", "maxResults": 1}
    if kind == "id":
        params["id"] = value
    elif kind == "handle":
        params["forHandle"] = value
    elif kind == "user":
        params["forUsername"] = value
    else:  # old /c/ custom URLs: one search call
        if search_calls_today(session) >= SEARCH_DAILY_LIMIT:
            raise YTError("YouTube search quota is used up for today — paste the channel’s @handle link instead.",
                          quota=True)
        data = yt_get(client, "search", {"part": "snippet", "type": "channel", "q": value, "maxResults": 1})
        _bump_search_calls(session)
        items = data.get("items") or []
        if not items:
            return None
        params["id"] = items[0]["id"]["channelId"]
    items = yt_get(client, "channels", params).get("items") or []
    return items[0] if items else None


def add_creator_from_url(session: Session, project_id: int, url: str, name: str = "", email: str = "",
                         notes: str = "") -> Creator:
    """The "Add creator" form: a YouTube/Twitch/any link and/or an email."""
    url, name, email = (url or "").strip(), (name or "").strip(), (email or "").strip()
    parsed = core.parse_creator_url(url) if url else None
    if url and parsed is None:
        raise ValueError("that link doesn’t look like a channel page")
    if parsed is None:
        if not email:
            raise ValueError("give a channel link or an email")
        parsed = {"platform": "other", "kind": "email", "value": "mailto:" + email.lower(), "url": None}
    if parsed["platform"] == "youtube":
        with httpx.Client(timeout=20) as client:
            item = resolve_youtube_channel(client, session, parsed)
            if item is None:
                raise ValueError("YouTube doesn’t know that channel")
            creator = apply_channel(session, item)
            creator.last_upload_at = rss_last_upload(client, creator.channel_id) or creator.last_upload_at
    else:
        channel_id = parsed["value"]
        creator = core.upsert_creator(session, parsed["platform"], channel_id,
                                      name=name or (parsed["value"] if parsed["kind"] == "login" else None)
                                      or email or channel_id,
                                      url=parsed["url"], handle=parsed["value"] if parsed["kind"] == "login" else None)
    if name and parsed["platform"] != "youtube":
        creator.name = name
    if email:
        core.add_emails(creator, core.extract_emails(email) or [email], "manual")
    if notes:
        creator.notes = ((creator.notes + "\n") if creator.notes else "") + notes[:500]
    creator.ignored = False
    session.add(creator)
    contact = core.ensure_contact(session, project_id, creator, manual=True)
    if contact.status in ("found", "skipped"):
        contact.status = "shortlist"
        session.add(contact)
    session.commit()
    core.rescore(session, project_id)
    return creator


# ── Link scanning (emails on linktree / personal sites) ──────────────────────
_SKIP_HOSTS = (
    "youtube.com", "youtu.be", "google.com", "twitch.tv", "twitter.com", "x.com", "t.co", "instagram.com",
    "tiktok.com", "facebook.com", "fb.com", "fb.me", "discord.gg", "discord.com", "discordapp.com",
    "patreon.com", "reddit.com", "steampowered.com", "steamcommunity.com", "epicgames.com", "gog.com",
    "amazon.com", "amazon.co.uk", "amazon.de", "amzn.to", "amzn.eu", "apple.com", "spotify.com",
    "threads.net", "bsky.app", "kick.com", "streamlabs.com", "streamelements.com", "paypal.me",
    "paypal.com", "ko-fi.com", "buymeacoffee.com", "teespring.com", "spring.com", "nordvpn.com",
    "surfshark.com", "g2a.com", "eneba.com", "kinguin.net", "humblebundle.com", "fanatical.com",
    "instant-gaming.com", "cdkeys.com", "gamivo.com", "raidshadowlegends.com", "plarium.com",
    "linkedin.com", "github.com", "soundcloud.com", "wikipedia.org", "medium.com", "tumblr.com",
    "imdb.com", "music.apple.com", "streamable.com", "gofundme.com", "throne.com", "throne.me",
    "vk.com", "vk.ru", "vkvideo.ru", "ok.ru", "t.me", "telegram.me", "boosty.to", "donationalerts.com",
    "dzen.ru", "rutube.ru", "nicovideo.jp", "bilibili.com", "weibo.com", "line.me",
)
_BIO_HOSTS = ("linktr.ee", "beacons.ai", "carrd.co", "bio.link", "solo.to", "lnk.bio", "linkin.bio", "msha.ke",
              "campsite.bio", "about.me", "allmylinks.com", "hoo.be", "snipfeed.co", "linkpop.com", "taplink.cc",
              "bento.me", "komi.io", "manylink.co", "linkr.bio", "tap.bio")


def _host_in(host: str, hosts: tuple) -> bool:
    return any(host == h or host.endswith("." + h) for h in hosts)


def scan_targets(links: list[str], limit: int = 3) -> list[str]:
    bio = [u for u in links if _host_in(core.host_of(u), _BIO_HOSTS)]
    other = [u for u in links if u not in bio and core.host_of(u) and not _host_in(core.host_of(u), _SKIP_HOSTS)]
    return (bio + other)[:limit]


def scan_creator_links(client: httpx.Client, links: list[str]) -> list[tuple[str, str]]:
    """[(email, page host)] from the creator's link-in-bio / personal site."""
    found: list[tuple[str, str]] = []
    for url in scan_targets(links):
        try:
            r = client.get(url, timeout=8)
        except (httpx.HTTPError, ValueError):
            continue
        host = core.host_of(str(r.url))
        if r.status_code != 200 or _host_in(host, _SKIP_HOSTS) or "html" not in r.headers.get("content-type", "html"):
            continue
        text = r.text[:1_500_000]
        emails = core.extract_emails(text)
        if not emails and not _host_in(host, _BIO_HOSTS):
            m = re.search(r'href="((?:https?://[^"]*)?/[^"]*(?:contact|about|business|press)[^"]*)"', text, re.I)
            if m:
                sub = httpx.URL(str(r.url)).join(html.unescape(m.group(1)))
                if core.host_of(str(sub)) == host:
                    try:
                        r2 = client.get(sub, timeout=8)
                        if r2.status_code == 200:
                            emails = core.extract_emails(r2.text[:1_500_000])
                    except httpx.HTTPError:
                        pass
        found += [(e, host) for e in emails if e not in {f[0] for f in found}]
        if found:
            break
    return found


# ── Background job (one at a time) ───────────────────────────────────────────
_lock = threading.Lock()
JOB: dict = {"running": False}
LABELS = {"suggest": "Finding similar games on Steam", "youtube": "Searching YouTube",
          "links": "Looking for emails on creators’ sites"}


def job_status() -> dict:
    return dict(JOB)


def _progress(message: Optional[str] = None, done: Optional[int] = None, total: Optional[int] = None) -> None:
    if message is not None:
        JOB["message"] = message
    if done is not None:
        JOB["done"] = done
    if total is not None:
        JOB["total"] = total


def start(kind: str, project_id: int, **kwargs) -> tuple[bool, str]:
    runners: dict[str, Callable] = {"suggest": run_suggest, "youtube": run_youtube, "links": run_links}
    if kind not in runners:
        return False, "unknown task"
    with _lock:
        if JOB.get("running"):
            return False, f"busy: {JOB.get('label')}"
        JOB.clear()
        JOB.update(running=True, kind=kind, project_id=project_id, label=LABELS[kind], message="Starting…",
                   done=0, total=0, error="", result="", started_at=time.time())

    def run() -> None:
        try:
            JOB["result"] = runners[kind](project_id, **kwargs)
        except YTError as exc:
            JOB["error"] = str(exc)
        except Exception as exc:
            logger.exception(f"outreach {kind} failed")
            JOB["error"] = f"{type(exc).__name__}: {exc}"[:300]
        finally:
            JOB.update(running=False, finished_at=time.time())

    threading.Thread(target=run, daemon=True, name=f"outreach-{kind}").start()
    return True, ""


def run_suggest(project_id: int) -> str:
    """Steam "More like this" for the game (or, with no store page, for the games
    already marked active) → suggestions scored by tag overlap with our game."""
    with Session(engine) as session, steam_client() as client:
        project = session.get(Project, project_id)
        existing = session.exec(select(OutreachGame).where(OutreachGame.project_id == project_id)).all()
        known = {g.steam_app_id for g in existing if g.steam_app_id}
        if project.steam_app_id:
            bases = [project.steam_app_id]
            own = store_items(client, bases).get(project.steam_app_id) or {}
            _tag_cache[project.steam_app_id] = own.get("tags") or {}
            details = steam_appdetails(client, project.steam_app_id)
            langs = core.steam_languages(details.get("supported_languages") or "")
            cfg = core.load_settings(session, project_id)
            if langs and cfg["languages"] == core.SETTINGS_DEFAULTS["languages"]:
                core.save_settings(session, project_id, {"languages": ",".join(langs)})
            if project.steam_app_id not in known:  # creators who already played our game
                session.add(OutreachGame(project_id=project_id, steam_app_id=project.steam_app_id,
                                         name=own.get("name") or project.name, source="own", status="suggested",
                                         similarity=1.0, reviews=own.get("reviews"), positive=own.get("positive"),
                                         tags_json=json.dumps(list(own.get("tags") or {})[:8])))
                known.add(project.steam_app_id)
        else:
            bases = [g.steam_app_id for g in existing if g.status == "active" and g.steam_app_id][:5]
            if not bases:
                return "This project has no Steam App ID — add a few similar games by hand first, then suggest more like them."
        own_tags = _project_tags(session, project_id, client)
        candidates: dict[int, str] = {}
        for i, base in enumerate(bases):
            _progress(f"Reading “More like this” ({i + 1}/{len(bases)})…")
            for appid, section in similar_appids(client, base):
                candidates.setdefault(appid, section)
        _progress(f"Reading Steam tags of {len(candidates)} games…")
        items = store_items(client, list(candidates))
        parents = [int(it["parent"]) for it in items.values() if it["type"] == "demo" and it.get("parent")]
        items.update(store_items(client, [p for p in parents if p not in items]))
        added = 0
        for appid in candidates:
            it = items.get(appid)
            if it and it["type"] == "demo" and it.get("parent"):  # a demo stands for its game
                appid = int(it["parent"])
                it = items.get(appid)
            if not it or appid in known:
                continue
            known.add(appid)
            session.add(OutreachGame(
                project_id=project_id, steam_app_id=appid, name=it["name"], source="steam_similar",
                status="suggested", similarity=tag_similarity(own_tags, it["tags"]) if own_tags and it["tags"] else None,
                reviews=it["reviews"], positive=it["positive"],
                tags_json=json.dumps(list(it["tags"])[:8]) if it["tags"] else None))
            added += 1
        session.commit()
    return f"{added} games suggested — tick the ones whose players would like your game."


def _refresh_language(session: Session, creator: Creator) -> None:
    vids = session.exec(select(CreatorVideo).where(CreatorVideo.creator_id == creator.id)).all()
    lang = core.guess_language(audio_langs=[v.language for v in vids], channel_lang=None,
                               titles=[v.title for v in vids], country=creator.country)
    if lang:
        creator.language = lang


def run_youtube(project_id: int, force: bool = False) -> str:
    """Search YouTube for the active similar games and turn the channels found into contacts."""
    with Session(engine) as session, httpx.Client(timeout=25, headers=UA) as client:
        cfg = core.load_settings(session, project_id)
        games = session.exec(select(OutreachGame).where(OutreachGame.project_id == project_id,
                                                        OutreachGame.status == "active")).all()
        todo = [g for g in games if force or g.searched_at is None]
        if not todo:
            return "Nothing to search: every game marked “use” was already searched (use “Search again”)."
        after = now_ts() - timedelta(days=365 * int(cfg["years"]))
        hits: dict[int, list[dict]] = {}
        stopped = ""
        calls_before = search_calls_today(session)
        _progress("Searching…", 0, len(todo))
        for i, g in enumerate(todo):
            if search_calls_today(session) >= SEARCH_DAILY_LIMIT:
                stopped = f" Stopped at the daily limit of {SEARCH_DAILY_LIMIT} searches — the rest go tomorrow."
                break
            _progress(f"Searching YouTube: {g.name}", i)
            query = g.search_query or f'"{g.name}"'
            variants = core.name_variants(g.name) + ([core.norm_name(g.search_query.replace('"', ""))]
                                                     if g.search_query else [])
            try:
                items = search_videos(session, client, query, int(cfg["pages"]), after)
            except YTError as exc:
                if exc.quota:
                    stopped = " " + str(exc)
                    break
                raise
            matched = [it for it in items if core.mentions_game(it["title"], variants)
                       or core.mentions_game(it["description"], variants)]
            hits[g.id] = matched
            g.searched_at, g.videos_found = now_ts(), len(matched)
            session.add(g)
            session.commit()
        calls = search_calls_today(session) - calls_before

        all_hits = [h for lst in hits.values() for h in lst]
        channel_ids = list(dict.fromkeys(h["channel_id"] for h in all_hits))
        known = {c.channel_id: c for c in session.exec(select(Creator).where(
            Creator.platform == "youtube", Creator.channel_id.in_(channel_ids or [""]))).all()}
        stale_before = now_ts() - timedelta(days=STALE_DAYS)
        need = [cid for cid in channel_ids if cid not in known or not known[cid].stats_at
                or known[cid].stats_at < stale_before]
        _progress(f"Reading {len(need)} channels…", len(todo))
        new_ids = {cid for cid in channel_ids if cid not in known}
        for cid, item in fetch_channels(client, need).items():
            known[cid] = apply_channel(session, item)
        session.commit()

        _progress(f"Reading {len(all_hits)} videos…")
        details = fetch_videos(client, [h["video_id"] for h in all_hits])
        touched: set[int] = set()
        for game_id, lst in hits.items():
            for h in lst:
                creator = known.get(h["channel_id"])
                if creator is None:
                    continue
                d = details.get(h["video_id"]) or {}
                sn, st = d.get("snippet") or {}, d.get("statistics") or {}
                v = session.exec(select(CreatorVideo).where(CreatorVideo.video_id == h["video_id"],
                                                            CreatorVideo.game_id == game_id)).first()
                v = v or CreatorVideo(creator_id=creator.id, game_id=game_id, video_id=h["video_id"], title=h["title"])
                v.title = html.unescape(sn.get("title") or h["title"])[:300]
                v.published_at = h["published_at"] or _parse_time(sn.get("publishedAt"))
                v.views = _int(st.get("viewCount"))
                v.language = (sn.get("defaultAudioLanguage") or sn.get("defaultLanguage") or None)
                session.add(v)
                desc = sn.get("description") or ""
                core.add_emails(creator, core.extract_emails(desc), f"video:{h['video_id']}")
                core.add_links(creator, core.extract_links(desc)[:10])
                touched.add(creator.id)
        session.commit()

        creators = [c for c in known.values() if c.id in touched]
        _progress("Checking when each channel last uploaded…")
        with ThreadPoolExecutor(max_workers=6) as pool:  # RSS is free: check every channel
            uploads = dict(zip([c.id for c in creators],
                               pool.map(lambda c: rss_last_upload(client, c.channel_id), creators)))
        for c in creators:
            if uploads.get(c.id):
                c.last_upload_at = uploads[c.id]
            _refresh_language(session, c)
            session.add(c)
            if not c.ignored:
                core.ensure_contact(session, project_id, c)
        session.commit()
        core.rescore(session, project_id)
        with_email = sum(1 for c in creators if core.emails_of(c))
        summary = (f"Searched {len(hits)} game{'s' if len(hits) != 1 else ''} ({calls} search calls): "
                   f"{len(all_hits)} videos from {len(creators)} channels, {len(new_ids)} new, "
                   f"{with_email} with an email.{stopped}")
    links = run_links(project_id, only_new=True)
    return summary + " " + links


def run_links(project_id: int, only_new: bool = True, limit: int = 80) -> str:
    """Open the link-in-bio / personal sites of creators without an email."""
    with Session(engine) as session:
        shared = core.shared_emails(session)
        contacts = session.exec(select(OutreachContact).where(
            OutreachContact.project_id == project_id,
            OutreachContact.status.not_in(["skipped", "declined"])).order_by(OutreachContact.score.desc())).all()
        creators = {c.id: c for c in session.exec(select(Creator).where(
            Creator.id.in_([k.creator_id for k in contacts] or [0]))).all()}
        todo = []
        for k in contacts:
            c = creators.get(k.creator_id)
            if (c and not c.ignored and not c.do_not_contact and not core.best_email(c, shared)
                    and scan_targets(core.links_of(c)) and (not only_new or c.links_scanned_at is None)):
                todo.append(c)
        todo = todo[:limit]
        if not todo:
            return "No new sites to check for emails."
        _progress(f"Opening the sites of {len(todo)} creators…", 0, len(todo))
        done = [0]

        def work(c: Creator) -> tuple[int, list]:
            with httpx.Client(timeout=10, follow_redirects=True, headers=UA) as client:
                res = scan_creator_links(client, core.links_of(c))
            done[0] += 1
            _progress(done=done[0])
            return c.id, res

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(work, todo))
        found = 0
        for cid, res in results:
            c = creators[cid]
            for email, host in res:
                found += core.add_emails(c, [email], f"link:{host}")
            c.links_scanned_at = now_ts()
            session.add(c)
        session.commit()
        core.rescore(session, project_id)
    return f"Checked the sites of {len(todo)} creators: {found} new email{'s' if found != 1 else ''}."


def refresh_stale(budget_s: float = 90.0) -> str:
    """Worker task: refresh YouTube data older than STALE_DAYS (policy: ≤30 days).
    When YouTube can't be reached for long, numbers older than 30 days are dropped."""
    deadline = time.time() + budget_s
    with Session(engine) as session:
        stale_before = now_ts() - timedelta(days=STALE_DAYS)
        rows = session.exec(select(Creator).where(Creator.platform == "youtube", Creator.stats_at.is_not(None),
                                                  Creator.stats_at < stale_before).limit(1000)).all()
        if not rows:
            return "outreach: nothing stale"
        refreshed = 0
        try:
            with httpx.Client(timeout=25, headers=UA) as client:
                for chunk in _chunks(rows):
                    if time.time() > deadline:
                        break
                    items = fetch_channels(client, [c.channel_id for c in chunk])
                    for c in chunk:
                        if c.channel_id in items:
                            apply_channel(session, items[c.channel_id])
                            refreshed += 1
                        else:  # channel gone
                            c.ignored, c.stats_at = True, now_ts()
                            session.add(c)
                    vids = session.exec(select(CreatorVideo).where(
                        CreatorVideo.creator_id.in_([c.id for c in chunk]))).all()
                    details = fetch_videos(client, [v.video_id for v in vids])
                    for v in vids:
                        st = (details.get(v.video_id) or {}).get("statistics") or {}
                        v.views = _int(st.get("viewCount"))
                        session.add(v)
                    session.commit()
        except YTError as exc:
            expired = now_ts() - timedelta(days=30)
            for c in rows:
                if c.stats_at and c.stats_at < expired:
                    c.subscribers = c.views = c.videos = None
                    session.add(c)
            session.commit()
            return f"outreach: refresh failed ({exc}); dropped numbers older than 30 days"
    return f"outreach: refreshed {refreshed} channels"
