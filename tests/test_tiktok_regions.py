"""Target regions of the TikTok test (app/services/tiktok_regions.py): presets, zones with summer time, the setting."""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.core import settings_schema as schema
from app.services import languages, tiktok_regions
from app.services.settings import DEFAULTS


def test_every_preset_is_well_formed():
    codes = [r.code for r in tiktok_regions.REGIONS]
    assert len(codes) == len(set(codes)) and set(codes) == set(tiktok_regions.BY_CODE)
    for r in tiktok_regions.REGIONS:
        assert re.fullmatch(r"[A-Z]{2}", r.code), r.code
        assert r.name and r.short and r.place, r.code
        assert r.lang in languages.NAMES, r.code
        ZoneInfo(r.tz)                                      # the tzdata package has it
        lo, hi = r.evening
        assert 0 <= lo < hi <= 24, r.code
        view = r.view()
        assert view["code"] == r.code and view["evening"] == [lo, hi] and view["lang_a"].split()[0] in ("a", "an")


def test_the_big_markets_are_there():
    for code in ("US", "BR", "MX", "GB", "DE", "FR", "ES", "IT", "PL", "TR", "AU", "JP", "KR", "TW", "ID", "TH", "VN",
                 "KZ"):
        assert tiktok_regions.get(code) is not None, code
    assert tiktok_regions.get("TW").lang == "zh-hant" and tiktok_regions.get("BR").lang == "pt-br"
    assert tiktok_regions.get("KZ").lang == "ru"


def test_names_read_well_where_the_page_puts_them():
    jp, us, de, gb = (tiktok_regions.get(c) for c in ("JP", "US", "DE", "GB"))
    assert (jp.clock, jp.at, jp.short, jp.place) == ("Time in Japan", "in Japan", "Japan", "Japan")
    assert (us.clock, us.at, us.short, us.place) == ("US Eastern time", "US Eastern time", "US", "the US")
    assert gb.view()["lang_a"] == "an English" and de.view()["lang_a"] == "a German"
    assert tiktok_regions.get("BR").view()["lang_word"] == "Portuguese"


def test_summer_time_moves_the_evening():
    us = tiktok_regions.get("US")
    july, january = datetime(2026, 7, 15, 23, 30), datetime(2027, 1, 15, 23, 30)   # the same UTC hour
    assert us.local(july).hour == 19 and us.in_evening(july)                        # EDT, UTC−4
    assert us.local(january).hour == 18 and not us.in_evening(january)              # EST, UTC−5
    jp = tiktok_regions.get("JP")                                                   # no summer time
    assert jp.local(datetime(2026, 7, 15, 12, 30)).hour == jp.local(datetime(2027, 1, 15, 12, 30)).hour == 21
    # a recent tz database: all of Kazakhstan is UTC+5 since March 2024
    assert tiktok_regions.get("KZ").local(datetime(2026, 7, 15)).utcoffset() == timedelta(hours=5)


def test_the_setting_is_a_select_of_the_presets_and_starts_empty():
    assert DEFAULTS["tiktok_region"] == ""
    assert tiktok_regions.of(DEFAULTS) is None and tiktok_regions.of({"tiktok_region": " jp "}).code == "JP"
    assert tiktok_regions.of({"tiktok_region": "Mars"}) is None
    f = schema.FIELDS["tiktok_region"]
    assert schema.OWNER["tiktok_region"] == "tiktok" and f.type == "select" and f.module == "tiktok_pipeline"
    choices = list(f.choices)
    assert choices[0][0] == "" and {c for c, _ in choices[1:]} == set(tiktok_regions.BY_CODE)
    assert [n for _, n in choices[1:]] == sorted(n for _, n in choices[1:])
