"""Creator outreach: similar games → creators who covered them → contacts → letters + keys.

This module holds the offline logic (parsing, scoring, keys, drafts, CSV import,
per-project settings, the /outreach page context). Network work — Steam, YouTube,
RSS, link scanning — lives in outreach_discovery.py.

Twitch is manual on purpose: its Developer Agreement forbids using Twitch API data
to target users with marketing, so streamers come from links you paste or a CSV.
"""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Iterable, Optional
from urllib.parse import quote, urlsplit

from sqlmodel import Session, select

from app.models.models import (Activity, Creator, CreatorVideo, KVState, OutreachContact, OutreachGame,
                               Project, SteamKey, now_ts)
from app.services import utm

STATUSES: dict[str, str] = {
    "found": "Found",
    "shortlist": "Shortlist",
    "drafted": "Drafted",
    "sent": "Sent",
    "replied": "Replied",
    "covered": "Covered",
    "declined": "Declined",
    "skipped": "Skipped",
}
# statuses after which the letter is out: the key stays with the creator
SENT_STATUSES = ("sent", "replied", "covered", "declined")

LANG_NAMES = {
    "en": "English", "ru": "Russian", "ja": "Japanese", "zh": "Chinese", "ko": "Korean",
    "de": "German", "fr": "French", "es": "Spanish", "pt": "Portuguese", "it": "Italian",
    "pl": "Polish", "tr": "Turkish", "uk": "Ukrainian", "ar": "Arabic", "th": "Thai",
    "vi": "Vietnamese", "id": "Indonesian", "nl": "Dutch", "cs": "Czech", "hu": "Hungarian",
    "sv": "Swedish", "no": "Norwegian", "da": "Danish", "fi": "Finnish", "ro": "Romanian",
}


def utc_now() -> datetime:
    return now_ts()


# ── Text parsing ─────────────────────────────────────────────────────────────
def norm_name(value: str) -> str:
    """'Surviving the Aftermath™: Deluxe' → 'surviving the aftermath deluxe'."""
    s = (value or "").lower().replace("&", " and ")
    s = re.sub(r"[™®©]", "", s)
    s = re.sub(r"[^\w]+", " ", s, flags=re.UNICODE)
    return re.sub(r"\s+", " ", s).strip()


def name_variants(name: str) -> list[str]:
    """Normalized forms a video title may use: the full name and, for 'Main: Subtitle'
    names, the main part when it has two words or more ('Stranded' alone is a word)."""
    full = norm_name(name)
    out = [full] if full else []
    for sep in (":", " - ", " – ", " — "):
        if sep in name:
            head = norm_name(name.split(sep, 1)[0])
            if head and head != full and " " in head:
                out.append(head)
    return out


def mentions_game(text: str, variants: Iterable[str]) -> bool:
    t = f" {norm_name(text)} "
    return any(v and f" {v} " in t for v in variants)


_EMAIL_RE = re.compile(r"(?<![\w.+-])[A-Za-z0-9][\w.+-]{0,63}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,24}(?![\w-])")
# "name [at] gmail [dot] com", "name(at)gmail(dot)com", "name {at} gmail.com"
_OBF_AT = re.compile(r"\s*[\[({<]\s*(?:at|@)\s*[\])}>]\s*", re.I)
_OBF_DOT = re.compile(r"\s*[\[({<]\s*(?:dot|\.)\s*[\])}>]\s*", re.I)
_BAD_TLDS = {"png", "jpg", "jpeg", "gif", "webp", "svg", "mp4", "js", "css", "avif"}
_BAD_DOMAINS = {"example.com", "example.org", "email.com", "domain.com", "yourdomain.com", "sentry.io",
                "wixpress.com", "sentry.wixpress.com", "sentry-next.wixpress.com", "mysite.com", "test.com"}
_BAD_LOCAL = {"your", "yourname", "name", "email", "user", "example", "noreply", "no-reply", "donotreply",
              "do-not-reply", "username", "someone", "john.doe", "you"}


def clean_email(email: str) -> Optional[str]:
    e = email.strip().strip(".").lower()
    if "@" not in e or len(e) > 120:
        return None
    local, domain = e.rsplit("@", 1)
    if domain.rsplit(".", 1)[-1] in _BAD_TLDS or domain in _BAD_DOMAINS or local in _BAD_LOCAL:
        return None
    if re.search(r"\d+x$", local):  # retina image names: logo@2x.png
        return None
    if re.fullmatch(r"[0-9a-f]{16,}", local) or re.fullmatch(r"[0-9a-f-]{32,}", local):
        return None  # tracking ids / hashes from page scripts, not mailboxes
    return e


def decode_cfemail(hexstr: str) -> Optional[str]:
    """Cloudflare 'email protection' obfuscation: first byte is the XOR key."""
    try:
        data = bytes.fromhex(hexstr)
        return "".join(chr(b ^ data[0]) for b in data[1:])
    except Exception:
        return None


def extract_emails(text: str) -> list[str]:
    """Email addresses in a description or a web page (plain, mailto:, lightly
    obfuscated, Cloudflare-protected), cleaned and de-duplicated in order."""
    if not text:
        return []
    found: list[str] = []
    candidates = [text, _OBF_DOT.sub(".", _OBF_AT.sub("@", text))]
    for hexstr in re.findall(r'data-cfemail="([0-9a-fA-F]+)"', text):
        dec = decode_cfemail(hexstr)
        if dec:
            candidates.append(dec)
    for chunk in candidates:
        for m in _EMAIL_RE.findall(chunk):
            e = clean_email(m)
            if e and e not in found:
                found.append(e)
    return found


_URL_RE = re.compile(r"https?://[^\s<>\"'()\[\]{}|\\^`]+", re.I)


def extract_links(text: str) -> list[str]:
    out: list[str] = []
    for m in _URL_RE.findall(text or ""):
        url = m.rstrip(".,;:!?…»")
        if url not in out:
            out.append(url)
    return out


def host_of(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


# ── Language ────────────────────────────────────────────────────────────────
_COUNTRY_LANG = {
    "RU": "ru", "BY": "ru", "KZ": "ru", "UA": "uk", "JP": "ja", "CN": "zh", "TW": "zh", "HK": "zh",
    "KR": "ko", "DE": "de", "AT": "de", "CH": "de", "FR": "fr", "BE": "fr", "ES": "es", "MX": "es",
    "AR": "es", "CO": "es", "CL": "es", "PE": "es", "BR": "pt", "PT": "pt", "IT": "it", "PL": "pl",
    "TR": "tr", "NL": "nl", "CZ": "cs", "HU": "hu", "SE": "sv", "NO": "no", "DK": "da", "FI": "fi",
    "RO": "ro", "TH": "th", "VN": "vi", "ID": "id", "EG": "ar", "SA": "ar", "JO": "ar", "AE": "ar",
    "US": "en", "GB": "en", "CA": "en", "AU": "en", "NZ": "en", "IE": "en", "ZA": "en", "IN": "en",
    "PH": "en", "SG": "en",
}


def script_lang(text: str) -> Optional[str]:
    """A language that the writing system alone gives away (titles of videos)."""
    if not text:
        return None
    if re.search(r"[぀-ヿ]", text):
        return "ja"
    if re.search(r"[가-힯]", text):
        return "ko"
    if re.search(r"[一-鿿]", text):
        return "zh"
    if re.search(r"[іїєґІЇЄҐ]", text):
        return "uk"
    if re.search(r"[Ѐ-ӿ]", text):
        return "ru"
    if re.search(r"[؀-ۿ]", text):
        return "ar"
    if re.search(r"[฀-๿]", text):
        return "th"
    return None


def guess_language(*, audio_langs: Iterable[Optional[str]] = (), channel_lang: Optional[str] = None,
                   titles: Iterable[str] = (), country: Optional[str] = None) -> Optional[str]:
    """Best guess of the language a channel speaks: the videos' audio language, then
    the channel default, then the script of the titles, then the country."""
    audio = [a.split("-")[0].lower() for a in audio_langs if a and a.lower() not in ("zxx", "und")]
    if audio:
        return Counter(audio).most_common(1)[0][0]
    if channel_lang:
        return channel_lang.split("-")[0].lower()
    scripts = [s for s in (script_lang(t) for t in titles) if s]
    if scripts:
        return Counter(scripts).most_common(1)[0][0]
    return _COUNTRY_LANG.get((country or "").upper())


_STEAM_LANG = {
    "english": "en", "russian": "ru", "japanese": "ja", "simplified chinese": "zh",
    "traditional chinese": "zh", "korean": "ko", "german": "de", "french": "fr",
    "spanish - spain": "es", "spanish - latin america": "es", "portuguese - brazil": "pt",
    "portuguese - portugal": "pt", "italian": "it", "polish": "pl", "turkish": "tr",
    "ukrainian": "uk", "thai": "th", "vietnamese": "vi", "dutch": "nl", "czech": "cs",
}


def steam_languages(supported: str) -> list[str]:
    """Steam appdetails `supported_languages` ('English<strong>*</strong>, Russian, …') → codes."""
    text = re.sub(r"<[^>]+>", "", supported or "").replace("*", "")
    text = text.split("languages with full audio support")[0]
    out: list[str] = []
    for part in text.split(","):
        code = _STEAM_LANG.get(part.strip().lower())
        if code and code not in out:
            out.append(code)
    return out


# ── Settings per project (kv_state "outreach:<id>") ──────────────────────────
SETTINGS_DEFAULTS: dict = {
    "pitch": "",             # what the game is and what's new — the letter is built on it
    "links": "",             # trailer / press kit, one per line
    "signature": "",         # how letters are signed
    "languages": "en",       # languages the game supports (scoring)
    "letter_languages": "en,ru,ja",  # write in the creator's language when it is one of these
    "include_key": True,
    "followup_days": 7,
    "years": 3,              # search videos from the last N years
    "pages": 2,              # YouTube result pages (50 videos each) per game
    "campaign": "outreach",  # utm_campaign of the store link in letters
}


def _kv_key(project_id: int) -> str:
    return f"outreach:{project_id}"


def load_settings(session: Session, project_id: int) -> dict:
    row = session.get(KVState, _kv_key(project_id))
    data = {}
    if row and row.value:
        try:
            data = json.loads(row.value)
        except ValueError:
            data = {}
    return {**SETTINGS_DEFAULTS, **{k: v for k, v in data.items() if k in SETTINGS_DEFAULTS}}


def save_settings(session: Session, project_id: int, values: dict) -> dict:
    cur = load_settings(session, project_id)
    for k, v in values.items():
        if k not in SETTINGS_DEFAULTS:
            continue
        default = SETTINGS_DEFAULTS[k]
        if isinstance(default, bool):
            v = v in (True, "1", "on", "true", 1)
        elif isinstance(default, int):
            try:
                v = int(v)
            except (TypeError, ValueError):
                v = default
        else:
            v = str(v or "").strip()
        cur[k] = v
    cur["pages"] = max(1, min(4, int(cur["pages"])))
    cur["years"] = max(1, min(10, int(cur["years"])))
    cur["followup_days"] = max(2, min(30, int(cur["followup_days"])))
    row = session.get(KVState, _kv_key(project_id)) or KVState(key=_kv_key(project_id))
    row.value = json.dumps(cur, ensure_ascii=False)
    row.updated_at = now_ts()
    session.add(row)
    session.commit()
    return cur


def csv_codes(value: str) -> list[str]:
    return [c.strip().lower() for c in (value or "").split(",") if c.strip()]


# ── Creators ────────────────────────────────────────────────────────────────
def emails_of(creator: Creator) -> list[dict]:
    try:
        return json.loads(creator.emails_json or "[]")
    except ValueError:
        return []


def links_of(creator: Creator) -> list[str]:
    try:
        return json.loads(creator.links_json or "[]")
    except ValueError:
        return []


def add_emails(creator: Creator, emails: Iterable[str], source: str) -> int:
    """Remember new addresses with where they were found; returns how many were new."""
    cur = emails_of(creator)
    known = {e["email"] for e in cur}
    added = 0
    for e in emails:
        e = clean_email(e) if e else None
        if e and e not in known:
            cur.append({"email": e, "source": source, "found_at": utc_now().strftime("%Y-%m-%d")})
            known.add(e)
            added += 1
    if added:
        creator.emails_json = json.dumps(cur, ensure_ascii=False)
    return added


def add_links(creator: Creator, links: Iterable[str]) -> None:
    cur = links_of(creator)
    for url in links:
        if url and url not in cur and len(cur) < 40:
            cur.append(url)
    creator.links_json = json.dumps(cur, ensure_ascii=False)


_BRAND_RE = re.compile(
    r"\b(playstation|xbox|nintendo|steam|ign|gamespot|gamesradar|pc ?gamer|gamingbolt|gametrailers|"
    r"trailers?|official|interactive|studios?|publishing|entertainment|paradox|devolver|tinybuild|team17|"
    r"ubisoft|bandai|sega|epic games|thq|gog|humble|news)\b", re.I)


def looks_like_brand(creator: Creator) -> bool:
    """Publisher / platform / news channels: they post trailers of everything."""
    return bool(_BRAND_RE.search(creator.name or "")) or (creator.subscribers or 0) >= 5_000_000


def shared_emails(session: Session, threshold: int = 3) -> set[str]:
    """Addresses found on several unrelated channels are sponsors' (raid@…), not the creator's."""
    counter: Counter = Counter()
    for raw in session.exec(select(Creator.emails_json).where(Creator.emails_json.is_not(None))).all():
        try:
            counter.update({e["email"] for e in json.loads(raw or "[]")})
        except (ValueError, TypeError, KeyError):
            continue
    return {e for e, n in counter.items() if n >= threshold}


def best_email(creator: Creator, shared: set[str] = frozenset()) -> Optional[dict]:
    """The address to write to: the channel's own description first, then video
    descriptions, then linked sites; sponsor addresses are skipped."""
    order = {"channel": 0, "manual": 0, "csv": 1, "video": 2, "link": 3}
    own = [e for e in emails_of(creator) if e["email"] not in shared and clean_email(e["email"])]
    if not own:
        return None
    return sorted(own, key=lambda e: order.get(e["source"].split(":")[0], 4))[0]


def parse_creator_url(raw: str) -> Optional[dict]:
    """A pasted channel link → {"platform", "kind", "value", "url"}.

    kind for YouTube: id (UC…) | handle (@name) | user | custom — resolved by the API later."""
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("@"):
        return {"platform": "youtube", "kind": "handle", "value": s, "url": f"https://www.youtube.com/{s}"}
    if not re.match(r"https?://", s, re.I):
        s = "https://" + s
    host = host_of(s)
    path = [p for p in urlsplit(s).path.split("/") if p]
    if host.endswith("youtube.com") or host == "youtu.be":
        if path and path[0].startswith("@"):
            return {"platform": "youtube", "kind": "handle", "value": path[0], "url": f"https://www.youtube.com/{path[0]}"}
        if len(path) >= 2 and path[0] == "channel" and path[1].startswith("UC"):
            return {"platform": "youtube", "kind": "id", "value": path[1],
                    "url": f"https://www.youtube.com/channel/{path[1]}"}
        if len(path) >= 2 and path[0] in ("user", "c"):
            return {"platform": "youtube", "kind": path[0], "value": path[1], "url": s}
        return None
    if host.endswith("twitch.tv"):
        if path and re.match(r"^\w{3,25}$", path[0]) and path[0] not in ("directory", "videos", "search"):
            login = path[0].lower()
            return {"platform": "twitch", "kind": "login", "value": login, "url": f"https://www.twitch.tv/{login}"}
        return None
    if host.endswith("kick.com") and path:
        return {"platform": "kick", "kind": "login", "value": path[0].lower(), "url": f"https://kick.com/{path[0].lower()}"}
    return {"platform": "other", "kind": "url", "value": s, "url": s}


def upsert_creator(session: Session, platform: str, channel_id: str, **fields) -> Creator:
    creator = session.exec(select(Creator).where(Creator.platform == platform,
                                                 Creator.channel_id == channel_id)).first()
    if creator is None:
        creator = Creator(platform=platform, channel_id=channel_id, name=fields.pop("name", None) or channel_id)
    for k, v in fields.items():
        if v is not None and hasattr(creator, k):
            setattr(creator, k, v)
    session.add(creator)
    session.flush()
    return creator


def ensure_contact(session: Session, project_id: int, creator: Creator, *, manual: bool = False,
                   status: Optional[str] = None) -> OutreachContact:
    contact = session.exec(select(OutreachContact).where(OutreachContact.project_id == project_id,
                                                         OutreachContact.creator_id == creator.id)).first()
    if contact is None:
        contact = OutreachContact(project_id=project_id, creator_id=creator.id, manual=manual)
        if status:
            contact.status = status
        elif looks_like_brand(creator) and not manual:
            contact.status = "skipped"
            contact.notes = "Auto-skipped: looks like a publisher, platform or news channel."
        session.add(contact)
        session.flush()
    elif manual and not contact.manual:
        contact.manual = True
        session.add(contact)
    return contact


# ── Scoring ─────────────────────────────────────────────────────────────────
def _size_points(subs: Optional[int]) -> tuple[int, str]:
    """Small and mid channels answer indie devs; the giants rarely do."""
    if subs is None:
        return 8, "size unknown"
    for limit, pts, label in ((300, 3, "under 300 subs"), (1_000, 10, "300–1K subs"),
                              (10_000, 20, "1K–10K subs"), (100_000, 18, "10K–100K subs"),
                              (500_000, 11, "100K–500K subs"), (2_000_000, 6, "500K–2M subs")):
        if subs < limit:
            return pts, label
    return 2, "2M+ subs"


def score_contact(creator: Creator, videos: list[tuple[CreatorVideo, OutreachGame]], *,
                  languages: list[str], has_email: bool, manual: bool, now: Optional[datetime] = None) -> tuple[float, list]:
    """0–100 and the parts it is made of, shown as the score's tooltip."""
    now = now or utc_now()
    parts: list[tuple[str, int]] = []
    games = {}
    for v, g in videos:
        games[g.id] = max(games.get(g.id, 0.0), g.similarity if g.similarity is not None else 0.7)
    if games:
        weighted = sum(0.6 + 0.4 * s for s in games.values())
        pts = round(45 * (1 - 0.45 ** weighted))
        parts.append((f"covered {len(games)} similar game{'s' if len(games) > 1 else ''}", pts))
    elif manual:
        parts.append(("added by hand", 30))
    dates = [v.published_at for v, _ in videos if v.published_at]
    if dates:
        age = (now - max(dates)).days
        pts = 20 if age <= 180 else 15 if age <= 365 else 9 if age <= 730 else 4
        parts.append((f"last relevant video {age // 30} mo ago" if age >= 60 else "relevant video this month or last", pts))
    if creator.last_upload_at:
        idle = (now - creator.last_upload_at).days
        if idle > 180:
            parts.append((f"no uploads for {idle // 30} months", -15))
    size_pts, size_label = _size_points(creator.subscribers)
    parts.append((size_label, size_pts))
    if has_email:
        parts.append(("has an email", 10))
    if creator.language:
        if creator.language in languages:
            parts.append((f"speaks {LANG_NAMES.get(creator.language, creator.language)}", 5))
        else:
            parts.append((f"{LANG_NAMES.get(creator.language, creator.language)} — not a game language", -5))
    else:
        parts.append(("language unknown", 2))
    if looks_like_brand(creator):
        parts.append(("looks like a brand channel", -30))
    total = max(0, min(100, sum(p for _, p in parts)))
    return float(total), parts


def project_videos(session: Session, project_id: int) -> dict[int, list[tuple[CreatorVideo, OutreachGame]]]:
    """creator_id → [(video, game)] for the project's active similar games."""
    games = {g.id: g for g in session.exec(select(OutreachGame).where(
        OutreachGame.project_id == project_id, OutreachGame.status == "active")).all()}
    out: dict[int, list] = {}
    if not games:
        return out
    for v in session.exec(select(CreatorVideo).where(CreatorVideo.game_id.in_(list(games)))).all():
        out.setdefault(v.creator_id, []).append((v, games[v.game_id]))
    return out


def rescore(session: Session, project_id: int) -> int:
    cfg = load_settings(session, project_id)
    langs = csv_codes(cfg["languages"]) or ["en"]
    vids = project_videos(session, project_id)
    shared = shared_emails(session)
    contacts = session.exec(select(OutreachContact).where(OutreachContact.project_id == project_id)).all()
    creators = {c.id: c for c in session.exec(select(Creator).where(
        Creator.id.in_([k.creator_id for k in contacts] or [0]))).all()}
    for k in contacts:
        cr = creators.get(k.creator_id)
        if cr is None:
            continue
        k.score, _ = score_contact(cr, vids.get(cr.id, []), languages=langs,
                                   has_email=bool(k.email or best_email(cr, shared)), manual=k.manual)
        session.add(k)
    session.commit()
    return len(contacts)


# ── Keys ────────────────────────────────────────────────────────────────────
_KEY_RE = re.compile(r"\b[A-Z0-9]{5}(?:-[A-Z0-9]{5}){2,4}\b")


def import_keys(session: Session, project_id: int, text: str, batch: str = "") -> tuple[int, int]:
    """Steam keys from the .txt Steamworks gives (or any text): (added, already known)."""
    keys = list(dict.fromkeys(_KEY_RE.findall((text or "").upper())))
    if not keys:
        return 0, 0
    known = set(session.exec(select(SteamKey.key).where(SteamKey.key.in_(keys))).all())
    batch = (batch or "").strip()[:80] or utc_now().strftime("batch %b %d")
    for k in keys:
        if k not in known:
            session.add(SteamKey(project_id=project_id, key=k, batch=batch))
    session.commit()
    return len(keys) - len(known), len(known)


def key_counts(session: Session, project_id: int) -> dict:
    rows = session.exec(select(SteamKey.status).where(SteamKey.project_id == project_id)).all()
    c = Counter(rows)
    return {"free": c["free"], "assigned": c["assigned"], "sent": c["sent"], "activated": c["activated"],
            "void": c["void"], "total": len(rows)}


def assign_key(session: Session, contact: OutreachContact) -> Optional[SteamKey]:
    """The contact's key, taking the oldest free one from the pool if it has none."""
    if contact.key_id:
        key = session.get(SteamKey, contact.key_id)
        if key and key.status != "void":
            return key
    key = session.exec(select(SteamKey).where(SteamKey.project_id == contact.project_id,
                                              SteamKey.status == "free").order_by(SteamKey.id)).first()
    if key is None:
        return None
    key.status, key.contact_id, key.assigned_at = "assigned", contact.id, utc_now()
    contact.key_id = key.id
    session.add(key)
    session.add(contact)
    return key


def release_key(session: Session, contact: OutreachContact) -> None:
    """Back to the pool — only while the letter hasn't gone out."""
    if not contact.key_id:
        return
    key = session.get(SteamKey, contact.key_id)
    if key and key.status == "assigned":
        key.status, key.contact_id, key.assigned_at = "free", None, None
        session.add(key)
    contact.key_id = None
    session.add(contact)


# ── Status changes ───────────────────────────────────────────────────────────
def _log_sent_activity(session: Session, project_id: int) -> None:
    """One chart marker per day: 'Outreach: emailed N creators'."""
    day = utc_now().strftime("%Y-%m-%d")
    start = datetime.strptime(day, "%Y-%m-%d")
    n = len(session.exec(select(OutreachContact.id).where(
        OutreachContact.project_id == project_id, OutreachContact.sent_at >= start,
        OutreachContact.sent_at < start + timedelta(days=1))).all())
    title = f"Outreach: emailed {n} creator{'s' if n != 1 else ''}"
    act = session.exec(select(Activity).where(Activity.project_id == project_id, Activity.day == day,
                                              Activity.title.startswith("Outreach: emailed"))).first()
    if act is None:
        act = Activity(project_id=project_id, day=day, title=title)
    act.title = title
    session.add(act)


def set_status(session: Session, contact: OutreachContact, status: str, *, coverage_url: str = "") -> None:
    if status not in STATUSES:
        raise ValueError("unknown status")
    now = utc_now()
    prev = contact.status
    contact.status = status
    key = session.get(SteamKey, contact.key_id) if contact.key_id else None
    if status == "sent":
        if prev not in SENT_STATUSES or not contact.sent_at:
            contact.sent_at = now
            cfg = load_settings(session, contact.project_id)
            contact.followup_at = now + timedelta(days=int(cfg["followup_days"]))
        if key and key.status == "assigned":
            key.status = "sent"
            session.add(key)
        session.add(contact)
        session.flush()
        _log_sent_activity(session, contact.project_id)
    elif status in ("replied", "covered", "declined"):
        contact.followup_at = None
        if status == "replied" and not contact.replied_at:
            contact.replied_at = now
        if status == "covered" and coverage_url:
            contact.coverage_url = coverage_url.strip()[:500]
        if key and key.status == "assigned":  # marked without "sent" first
            key.status = "sent"
            session.add(key)
    elif status in ("skipped", "found", "shortlist"):
        if prev not in SENT_STATUSES:
            release_key(session, contact)
    session.add(contact)
    session.commit()


# ── Letters ─────────────────────────────────────────────────────────────────
KEY_TOKEN = "{KEY}"

_SYSTEM_PROMPT = """You write cold outreach emails from a solo indie game developer to a YouTube or Twitch creator. The goal is that the creator tries the game; coverage is welcome but never demanded.

Rules:
- Plain text. 80–140 words in the body. No emojis, no bullet lists, no hype words ("amazing", "revolutionary", "unique").
- Open by greeting the creator by name, then refer to ONE of their videos by its title and say in a sentence why this game fits their channel (the similar game they played).
- One or two sentences on what the game is and what is new, using only facts from the pitch. Never invent features, prices, dates or numbers, and never mention that some information is missing.
- If a key is included, put the literal placeholder {KEY} on its own line after a short label in the letter's language ("Steam key: {KEY}", "Ключ Steam: {KEY}", "Steamキー：{KEY}"). Never write a real key.
- Include the store link once.
- Say clearly there is no obligation to cover the game.
- Sign with the signature exactly as given.
- After the signature add one short line in parentheses: where you found their address (given below) and that one reply is enough to never hear from you again.
- Japanese: polite です/ます style, start with はじめまして. Russian: address the creator with «вы».

Output exactly:
SUBJECT: <subject, at most 9 words, no clickbait, mention the game name>

<body>"""


def where_found_phrase(source: str) -> str:
    kind = (source or "").split(":")[0]
    return {"channel": "the description of your YouTube channel",
            "video": "the description of one of your videos",
            "link": "a site linked from your channel",
            "csv": "a public creator directory",
            "manual": "your public channel page"}.get(kind, "your public channel page")


def store_link(project: Project, creator: Creator, cfg: dict) -> str:
    if not project.steam_app_id:
        return ""
    source = creator.platform if creator.platform in ("youtube", "twitch") else "email"
    content = utm.slug(creator.handle or creator.name or "")
    return utm.build_url(utm.steam_base_url(project.steam_app_id), source, "outreach",
                         utm.slug(cfg.get("campaign") or "outreach"), content)


def letter_language(creator: Creator, cfg: dict) -> str:
    allowed = csv_codes(cfg.get("letter_languages", "en")) or ["en"]
    return creator.language if creator.language in allowed else "en"


def letter_facts(session: Session, contact: OutreachContact) -> dict:
    """Everything the letter may mention; the key itself never leaves the app."""
    project = session.get(Project, contact.project_id)
    creator = session.get(Creator, contact.creator_id)
    cfg = load_settings(session, contact.project_id)
    vids = sorted(project_videos(session, contact.project_id).get(creator.id, []),
                  key=lambda vg: ((vg[0].views or 0), vg[0].published_at or datetime.min), reverse=True)
    email = contact.email or (best_email(creator) or {}).get("email") or ""
    src = next((e["source"] for e in emails_of(creator) if e["email"] == email), "manual")
    return {
        "creator_name": creator.name,
        "platform": creator.platform,
        "subscribers": creator.subscribers,
        "their_videos": [{"title": v.title, "about_game": g.name,
                          "published": v.published_at.strftime("%Y-%m") if v.published_at else None}
                         for v, g in vids[:3]],
        "similar_games_they_played": list(dict.fromkeys(g.name for _, g in vids))[:5],
        "our_game": project.name,
        "store_link": store_link(project, creator, cfg),
        "pitch": cfg["pitch"],
        "other_links": [ln.strip() for ln in cfg["links"].splitlines() if ln.strip()],
        "signature": cfg["signature"] or "the developer",
        "include_key": bool(cfg["include_key"]),
        "where_found": where_found_phrase(src),
        "language": LANG_NAMES.get(letter_language(creator, cfg), "English"),
    }


def parse_letter(text: str) -> tuple[str, str]:
    text = (text or "").strip().replace("\r\n", "\n")
    m = re.match(r"^\s*(?:\*\*)?subject(?:\*\*)?\s*:\s*(.+?)\s*\n", text, re.I)
    if not m:
        return "", text
    return m.group(1).strip().strip("*").strip(), text[m.end():].strip()


def fallback_letter(f: dict) -> tuple[str, str]:
    """A plain template for when OpenAI isn't configured."""
    video = (f["their_videos"] or [{}])[0]
    games = f["similar_games_they_played"]
    lines = [f"Hi {f['creator_name']},", ""]
    if video.get("title"):
        lines.append(f"I watched your video “{video['title']}” — since you played {video['about_game']}, "
                     f"I think {f['our_game']} could be your kind of game.")
    elif games:
        lines.append(f"Since you played {', '.join(games[:2])}, I think {f['our_game']} could be your kind of game.")
    else:
        lines.append(f"I make {f['our_game']} and I think it could suit your channel.")
    if f["pitch"]:
        lines += ["", f["pitch"]]
    if f["store_link"]:
        lines += ["", f["store_link"]]
    if f["include_key"]:
        lines += ["", f"Steam key: {KEY_TOKEN}"]
    lines += ["", "No obligation to cover it at all — I’d just love to hear what you think.", "",
              f["signature"], "",
              f"(I found your address in {f['where_found']}. Reply “no” and I won’t write again.)"]
    return f"{f['our_game']} — a key for you", "\n".join(lines)


def generate_letter(session: Session, contact: OutreachContact) -> dict:
    """Draft subject + body (the AI from Settings — OpenAI or Claude — when configured,
    otherwise a template), put the contact's key in place of {KEY}, save it on the
    contact and return it."""
    from app.services import llm

    facts = letter_facts(session, contact)
    warnings: list[str] = []
    if not facts["pitch"]:
        from app.services.outreach_discovery import store_blurb
        project = session.get(Project, contact.project_id)
        facts["pitch"] = store_blurb(project.steam_app_id) if project.steam_app_id else ""
        warnings.append("The pitch is empty — the letter describes the game from its Steam page. "
                        "Add what’s new in Letter settings.")
    if facts["signature"] == "the developer":
        warnings.append("No signature yet — set one in Letter settings.")
    subject = body = ""
    model = None
    if llm.provider():
        try:
            answer, model = llm.chat(
                [{"role": "system", "content": _SYSTEM_PROMPT},
                 {"role": "user", "content": f"Write the email in {facts['language']}.\n\n"
                                             + json.dumps(facts, ensure_ascii=False, indent=1)}],
                temperature=0.7, max_tokens=900, timeout=60)
            subject, body = parse_letter(answer)
        except llm.LLMError as exc:
            warnings.append(f"The AI failed ({exc}) — used the plain template.")
        except Exception as exc:  # network down etc.
            warnings.append(f"The AI failed ({type(exc).__name__}) — used the plain template.")
    else:
        warnings.append("No AI key in Settings (OpenAI or Claude) — used the plain template.")
    if not body:
        subject, body = fallback_letter(facts)
    if facts["include_key"]:
        key = assign_key(session, contact)
        if key is None:
            warnings.append("No free keys — import a batch below; {KEY} stays in the text.")
        elif KEY_TOKEN in body:
            body = body.replace(KEY_TOKEN, key.key)
        else:
            body += f"\n\nSteam key: {key.key}"
    else:
        body = "\n".join(line for line in body.splitlines() if KEY_TOKEN not in line)
    contact.subject, contact.body = subject[:300], body
    if not contact.email:
        creator = session.get(Creator, contact.creator_id)
        contact.email = (best_email(creator, shared_emails(session)) or {}).get("email")
    if contact.status in ("found", "shortlist"):
        contact.status = "drafted"
    session.add(contact)
    session.commit()
    return {"subject": contact.subject, "body": contact.body, "warnings": warnings, "model": model}


def followup_text(session: Session, contact: OutreachContact) -> tuple[str, str]:
    creator = session.get(Creator, contact.creator_id)
    project = session.get(Project, contact.project_id)
    cfg = load_settings(session, contact.project_id)
    lang = letter_language(creator, cfg)
    sig = cfg["signature"] or ""
    if lang == "ru":
        body = (f"Здравствуйте, {creator.name}!\n\nНапоминаю о письме про {project.name} — вдруг оно потерялось. "
                f"Ключ из письма ниже всё ещё действует. Если не ваше — ничего страшного, больше не побеспокою.\n\n{sig}")
    elif lang == "ja":
        body = (f"{creator.name}様\n\n先日お送りした{project.name}のメールが埋もれていないか、念のためご連絡しました。"
                f"下のメールのキーはまだ使えます。ご興味がなければ、このままで大丈夫です。\n\n{sig}")
    else:
        body = (f"Hi {creator.name},\n\nJust bumping this in case it got buried — the key below still works. "
                f"If {project.name} isn’t a fit, no worries at all and I won’t follow up again.\n\n{sig}")
    subject = f"Re: {contact.subject}" if contact.subject else project.name
    return subject, body.strip()


def gmail_compose_url(to: str, subject: str, body: str) -> str:
    return ("https://mail.google.com/mail/?view=cm&fs=1&to=" + quote(to or "") + "&su=" + quote(subject or "")
            + "&body=" + quote(body or ""))


# ── CSV import (CreatorScout, IMPRESS, spreadsheets) ─────────────────────────
_CSV_FIELDS = {
    "name": ("name", "creator", "channel name", "channel", "title", "streamer", "display name"),
    "url": ("url", "link", "channel url", "channel link", "profile", "youtube", "twitch", "channel_url"),
    "email": ("email", "e-mail", "mail", "contact email", "business email", "published email"),
    "subscribers": ("subscribers", "subs", "followers", "audience", "subscriber count", "follower count"),
    "language": ("language", "lang"),
    "country": ("country",),
    "notes": ("notes", "games", "comparables", "covered", "comment", "comments", "similar games"),
}


def _csv_column(headers: list[str], names: tuple) -> Optional[int]:
    low = [h.strip().lower() for h in headers]
    for n in names:
        if n in low:
            return low.index(n)
    for i, h in enumerate(low):
        if any(n in h for n in names if len(n) > 3):
            return i
    return None


def _to_int(value: str) -> Optional[int]:
    s = (value or "").strip().lower().replace(",", "").replace(" ", "")
    m = re.match(r"^(\d+(?:\.\d+)?)([km]?)$", s)
    if not m:
        return None
    n = float(m.group(1)) * {"": 1, "k": 1_000, "m": 1_000_000}[m.group(2)]
    return int(n)


def import_csv(session: Session, project_id: int, text: str, filename: str = "") -> dict:
    """Rows with at least a channel link or a name+email become creators + contacts."""
    text = text.lstrip("﻿")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    if not rows:
        return {"added": 0, "updated": 0, "skipped": 0}
    headers = rows[0]
    col = {f: _csv_column(headers, names) for f, names in _CSV_FIELDS.items()}
    if col["url"] is None and col["name"] is None:
        raise ValueError("the CSV needs a column with channel links or names")
    source = "csv:" + (filename or "import")[:60]
    added = updated = skipped = 0

    def cell(row, field):
        i = col[field]
        return row[i].strip() if i is not None and i < len(row) else ""

    for row in rows[1:]:
        url, name, email = cell(row, "url"), cell(row, "name"), cell(row, "email")
        parsed = parse_creator_url(url) if url else None
        if parsed:
            platform = parsed["platform"]
            channel_id = parsed["value"] if parsed["kind"] in ("id", "login") else parsed["url"]
        elif name and email:
            platform, channel_id = "other", "mailto:" + email.lower()
        else:
            skipped += 1
            continue
        existing = session.exec(select(Creator).where(Creator.platform == platform,
                                                      Creator.channel_id == channel_id)).first()
        creator = upsert_creator(session, platform, channel_id,
                                 name=name or (existing.name if existing else None) or channel_id,
                                 url=(parsed or {}).get("url") or (existing.url if existing else None),
                                 handle=parsed["value"] if parsed and parsed["kind"] in ("handle", "login") else None,
                                 subscribers=_to_int(cell(row, "subscribers")) or (existing.subscribers if existing else None),
                                 language=(cell(row, "language")[:2].lower() or None),
                                 country=(cell(row, "country")[:2].upper() or None))
        add_emails(creator, extract_emails(email) or ([email] if "@" in email else []), source)
        note = cell(row, "notes")
        if note and note not in (creator.notes or ""):
            creator.notes = ((creator.notes + "\n") if creator.notes else "") + note[:500]
        session.add(creator)
        ensure_contact(session, project_id, creator, manual=True)
        updated += existing is not None
        added += existing is None
    session.commit()
    rescore(session, project_id)
    return {"added": added, "updated": updated, "skipped": skipped}


# ── Page context ────────────────────────────────────────────────────────────
def research_links(game: OutreachGame) -> list[dict]:
    """Where to look by hand for streamers of a game (Twitch data can't be pulled)."""
    slug_us = re.sub(r"[^a-z0-9]+", "_", game.name.lower()).strip("_")
    q = quote(game.name)
    return [
        {"label": "SullyGnome", "url": f"https://sullygnome.com/game/{slug_us}/365/watched",
         "hint": "Twitch channels that streamed it most in the last year"},
        {"label": "Twitch", "url": f"https://www.twitch.tv/search?term={q}", "hint": "live and recent streams"},
        {"label": "YouTube", "url": f"https://www.youtube.com/results?search_query={quote(chr(34) + game.name + chr(34))}",
         "hint": "videos (opens YouTube search)"},
    ]


def contact_row(contact: OutreachContact, creator: Creator, vids: list, shared: set[str],
                languages: list[str]) -> dict:
    score, parts = score_contact(creator, vids, languages=languages,
                                 has_email=bool(contact.email or best_email(creator, shared)), manual=contact.manual)
    games = {}
    for v, g in sorted(vids, key=lambda vg: vg[0].views or 0, reverse=True):
        games.setdefault(g.name, v)
    last = max((v.published_at for v, _ in vids if v.published_at), default=None)
    email = contact.email or (best_email(creator, shared) or {}).get("email")
    return {"c": contact, "cr": creator, "score": int(score),
            "score_parts": [f"{'+' if p >= 0 else '−'}{abs(p)} {label}" for label, p in parts],
            "games": [{"name": n, "video_id": v.video_id, "title": v.title, "views": v.views} for n, v in games.items()],
            "last_video": last, "email": email, "status_label": STATUSES.get(contact.status, contact.status),
            "followup_due": bool(contact.status == "sent" and contact.followup_at and contact.followup_at <= utc_now()
                                 and contact.followups == 0)}


def page_context(session: Session, project_id: int) -> dict:
    projects = [p for p in session.exec(select(Project).order_by(Project.sort_order, Project.id)).all()
                if (p.status or "active") != "archived"]
    projects.sort(key=lambda p: p.status == "paused")
    project = next((p for p in projects if p.id == project_id), None)
    if project is None:
        project = next((p for p in projects if p.steam_app_id), projects[0] if projects else None)
    if project is None:
        return {"projects": [], "project": None}
    cfg = load_settings(session, project.id)
    games = session.exec(select(OutreachGame).where(OutreachGame.project_id == project.id)).all()
    games.sort(key=lambda g: ({"active": 0, "suggested": 1, "rejected": 2}.get(g.status, 3),
                              -(g.similarity if g.similarity is not None else 0.7), g.name.lower()))
    contacts = session.exec(select(OutreachContact).where(OutreachContact.project_id == project.id)).all()
    creators = {c.id: c for c in session.exec(select(Creator).where(
        Creator.id.in_([k.creator_id for k in contacts] or [0]))).all()}
    vids = project_videos(session, project.id)
    shared = shared_emails(session)
    langs = csv_codes(cfg["languages"]) or ["en"]
    rows = []
    for k in contacts:
        cr = creators.get(k.creator_id)
        if cr is None or cr.ignored:
            continue
        rows.append(contact_row(k, cr, vids.get(cr.id, []), shared, langs))
    rows.sort(key=lambda r: (-r["score"], r["cr"].name.lower()))
    counts = Counter(r["c"].status for r in rows)
    return {
        "projects": projects, "project": project, "cfg": cfg, "games": games,
        "active_games": [g for g in games if g.status == "active"],
        "rows": rows, "status_counts": counts, "statuses": STATUSES,
        "with_email": sum(1 for r in rows if r["email"]),
        "followups_due": sum(1 for r in rows if r["followup_due"]),
        "keys": key_counts(session, project.id),
        "languages_seen": sorted({r["cr"].language for r in rows if r["cr"].language}),
        "lang_names": LANG_NAMES, "research_links": research_links,
    }
