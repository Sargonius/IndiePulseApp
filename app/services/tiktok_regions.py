"""Target regions of the TikTok test (🎵 TikTok, app/services/tiktok_pipeline.py): the country the clips are aimed
at. Settings → TikTok keeps its code (`tiktok_region`); empty = not chosen yet, and the page then names no country.

A region is its ISO code, its name, the IANA zone its evening is judged in (zoneinfo: the tzdata package, since
Windows has no tz database of its own), the language most of its viewers read (a code of languages.NAMES) and the
evening hours when TikTok is busiest there. A country with several zones is judged in its most populous one, and
the page says which (`zone`). Plain data: a new region is one more line.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from app.services import languages

EVENING = (19, 23)   # hours of the day when TikTok is busiest (the playbook's rule of thumb)


@dataclass(frozen=True)
class Region:
    code: str                  # ISO 3166-1 alpha-2
    name: str                  # in the lists, and the region in TikTok Creative Center
    tz: str                    # IANA zone
    lang: str                  # the main language
    short: str = ""            # "{short} share of viewers" (default: the name)
    place: str = ""            # after "in" / "from": "the US" (default: the name)
    zone: str = ""             # which time, for a country with several zones: "US Eastern time"
    evening: tuple = EVENING

    def __post_init__(self):
        object.__setattr__(self, "short", self.short or self.name)
        object.__setattr__(self, "place", self.place or self.name)

    @property
    def at(self) -> str:
        """After a time of day: "21:30 in Japan", "21:30 US Eastern time"."""
        return self.zone or f"in {self.place}"

    @property
    def clock(self) -> str:
        """Above the clock: "Time in Japan", "US Eastern time"."""
        return self.zone or f"Time in {self.place}"

    def local(self, when: datetime) -> datetime:
        """A UTC time (naive, as the database keeps it) in the region's zone, its summer time included."""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return when.astimezone(ZoneInfo(self.tz))

    def in_evening(self, when: datetime) -> bool:
        lo, hi = self.evening
        return lo <= self.local(when).hour < hi

    def view(self) -> dict:
        """What the page draws with (JSON-ready)."""
        word = languages.name(self.lang).split(" (")[0]   # "Portuguese (Brazil)" → "Portuguese", for prose
        return {"code": self.code, "name": self.name, "short": self.short, "place": self.place, "at": self.at,
                "clock": self.clock, "tz": self.tz, "lang": self.lang, "lang_name": languages.name(self.lang),
                "lang_word": word, "lang_a": ("an " if word[:1] in "AEIOU" else "a ") + word,
                "evening": list(self.evening)}


REGIONS: tuple[Region, ...] = (
    # Americas
    Region("US", "United States", "America/New_York", "en", short="US", place="the US", zone="US Eastern time"),
    Region("BR", "Brazil", "America/Sao_Paulo", "pt-br", zone="Brasília time"),
    Region("MX", "Mexico", "America/Mexico_City", "es", zone="Mexico City time"),
    # Europe
    Region("GB", "United Kingdom", "Europe/London", "en", short="UK", place="the UK"),
    Region("DE", "Germany", "Europe/Berlin", "de"),
    Region("FR", "France", "Europe/Paris", "fr"),
    Region("ES", "Spain", "Europe/Madrid", "es"),
    Region("IT", "Italy", "Europe/Rome", "it"),
    Region("PL", "Poland", "Europe/Warsaw", "pl"),
    Region("TR", "Turkey", "Europe/Istanbul", "tr"),
    # Asia-Pacific
    Region("AU", "Australia", "Australia/Sydney", "en", zone="Sydney time"),
    Region("JP", "Japan", "Asia/Tokyo", "ja"),
    Region("KR", "South Korea", "Asia/Seoul", "ko"),
    Region("TW", "Taiwan", "Asia/Taipei", "zh-hant"),
    Region("ID", "Indonesia", "Asia/Jakarta", "id", zone="Jakarta time"),
    Region("TH", "Thailand", "Asia/Bangkok", "th"),
    Region("VN", "Vietnam", "Asia/Ho_Chi_Minh", "vi"),
    Region("KZ", "Kazakhstan", "Asia/Almaty", "ru"),   # one zone since 2024; Russian is what most of it reads
)
BY_CODE: dict[str, Region] = {r.code: r for r in REGIONS}


def get(code) -> Optional[Region]:
    return BY_CODE.get(str(code or "").strip().upper())


def of(cfg: dict) -> Optional[Region]:
    """The target region of the settings, None until one is picked."""
    return get((cfg or {}).get("tiktok_region"))


def choices() -> tuple:
    """(code, name) pairs for a select, by name; "" = not chosen yet."""
    return (("", "Not chosen yet"),) + tuple((r.code, r.name) for r in sorted(REGIONS, key=lambda r: r.name))
