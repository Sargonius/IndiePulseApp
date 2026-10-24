"""Creator outreach: parsing, scoring, keys, letters, CSV, YouTube discovery (mocked)."""
from datetime import datetime, timedelta

import httpx
import pytest
from sqlmodel import select

from app.models.models import Activity, Creator, CreatorVideo, OutreachContact, OutreachGame, SteamKey
from app.services import outreach as core
from app.services import outreach_discovery as disc


@pytest.fixture(autouse=True)
def no_store_calls(monkeypatch):
    """An empty pitch falls back to the Steam short description: no network in tests."""
    monkeypatch.setattr(disc, "store_blurb", lambda appid: "A zombie city builder.")


# ── parsing ──────────────────────────────────────────────────────────────────
def test_extract_emails_variants_and_noise():
    key = 0x42
    hexed = format(key, "02x") + "".join(format(ord(c) ^ key, "02x") for c in "biz@creator.tv")
    text = ("Business inquiries: Foo.Bar@Gmail.com\n"
            "jake [at] proton [dot] me / jake(at)mail(dot)ru\n"
            f'<a data-cfemail="{hexed}">[email protected]</a>\n'
            "logo@2x.png you@example.com x@sentry.wixpress.com name@domain.com")
    assert core.extract_emails(text) == ["foo.bar@gmail.com", "jake@proton.me", "jake@mail.ru", "biz@creator.tv"]


def test_links_and_scan_targets_prefer_link_in_bio():
    links = core.extract_links("Twitter https://x.com/foo, merch: https://teespring.com/foo. "
                               "Site https://foo.dev/ and https://linktr.ee/foo!")
    assert links == ["https://x.com/foo", "https://teespring.com/foo", "https://foo.dev/", "https://linktr.ee/foo"]
    assert disc.scan_targets(links) == ["https://linktr.ee/foo", "https://foo.dev/"]


def test_guess_language_priority():
    assert core.guess_language(audio_langs=["ru", "ru", "en"], channel_lang="en", country="US") == "ru"
    assert core.guess_language(channel_lang="ja-JP", country="US") == "ja"
    assert core.guess_language(titles=["Строим город", "Day 1"], country="US") == "ru"
    assert core.guess_language(titles=["ゾンビの街"]) == "ja"
    assert core.guess_language(country="br") == "pt"
    assert core.guess_language() is None


def test_name_matching():
    v = core.name_variants("Stranded: Alien Dawn")
    assert core.mentions_game("Let's play STRANDED - Alien Dawn ep 1", v)
    assert core.mentions_game("Stranded Alien Dawn is great", v)
    assert not core.mentions_game("I was stranded on an island", v)
    assert core.mentions_game("Surviving the Aftermath™ review", core.name_variants("Surviving the Aftermath"))


def test_steam_languages():
    s = "English<strong>*</strong>, Russian, Japanese, Simplified Chinese<br><strong>*</strong>languages with full audio support"
    assert core.steam_languages(s) == ["en", "ru", "ja", "zh"]


def test_parse_creator_url():
    assert core.parse_creator_url("https://www.youtube.com/@Foo")["kind"] == "handle"
    assert core.parse_creator_url("youtube.com/channel/UCabc123")["value"] == "UCabc123"
    t = core.parse_creator_url("https://www.twitch.tv/SomeOne/videos")
    assert (t["platform"], t["value"]) == ("twitch", "someone")
    assert core.parse_creator_url("https://www.youtube.com/watch?v=1") is None
    assert core.parse_creator_url("https://foo.dev")["platform"] == "other"


def test_parse_letter():
    assert core.parse_letter("SUBJECT: Hello there\n\nHi Bob,\nText") == ("Hello there", "Hi Bob,\nText")
    assert core.parse_letter("**Subject:** Key for you\nBody") == ("Key for you", "Body")
    assert core.parse_letter("No subject line") == ("", "No subject line")


def test_pacific_day_follows_us_dst():
    assert disc.pacific_day(datetime(2026, 7, 1, 6, 59)) == "2026-06-30"  # PDT, UTC-7
    assert disc.pacific_day(datetime(2026, 7, 1, 7, 0)) == "2026-07-01"
    assert disc.pacific_day(datetime(2026, 12, 1, 7, 30)) == "2026-11-30"  # PST, UTC-8
    assert disc.pacific_day(datetime(2026, 12, 1, 8, 0)) == "2026-12-01"


def test_tag_similarity():
    a = {"City Builder": 200, "Zombies": 190, "Survival": 150}
    assert disc.tag_similarity(a, a) == 1.0
    assert disc.tag_similarity(a, {"Racing": 100}) == 0.0
    assert 0 < disc.tag_similarity(a, {"City Builder": 100, "Medieval": 90}) < 1


def test_similar_appids_parses_sections():
    page = ('<h2>Project X</h2><div data-ds-appid="5"></div>'
            '<h2>Looking for similar items</h2><a data-ds-appid="10"></a><a data-ds-appid="11"></a>'
            '<h2>Top Sellers</h2><a data-ds-appid="11"></a><a data-ds-appid="12"></a>')
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, text=page)))
    assert disc.similar_appids(client, 5) == [(10, "Similar"), (11, "Similar"), (12, "Top Sellers")]


def test_store_items_tags_reviews_and_demo_parent(monkeypatch):
    monkeypatch.setattr(disc, "_tag_names", {})

    def handler(req):
        if "GetTagList" in str(req.url):
            return httpx.Response(200, json={"response": {"tags": [{"tagid": 1, "name": "Zombies"},
                                                                   {"tagid": 2, "name": "City Builder"}]}})
        return httpx.Response(200, json={"response": {"store_items": [
            {"appid": 10, "name": "Game", "type": 0, "tags": [{"tagid": 2, "weight": 600}, {"tagid": 1, "weight": 500}],
             "reviews": {"summary_filtered": {"review_count": 437, "percent_positive": 75}}},
            {"appid": 11, "name": "Game Demo", "type": 1, "related_items": {"parent_appid": 10}},
        ]}})
    client = httpx.Client(transport=httpx.MockTransport(handler))
    items = disc.store_items(client, [10, 11])
    assert items[10] == {"name": "Game", "type": "game", "parent": None, "tags": {"City Builder": 600, "Zombies": 500},
                         "reviews": 437, "positive": 75}
    assert items[11]["type"] == "demo" and items[11]["parent"] == 10


# ── scoring ──────────────────────────────────────────────────────────────────
def _game(id_, sim=0.8):
    return OutreachGame(id=id_, project_id=1, name=f"G{id_}", similarity=sim, status="active")


def test_mid_size_relevant_creator_beats_giant_brand():
    now = datetime(2026, 10, 1)
    vids = [(CreatorVideo(creator_id=1, game_id=1, video_id="a", title="t", published_at=now - timedelta(days=40)), _game(1)),
            (CreatorVideo(creator_id=1, game_id=2, video_id="b", title="t", published_at=now - timedelta(days=90)), _game(2))]
    mid = Creator(platform="youtube", channel_id="A", name="Cozy Builder", subscribers=12_000, language="en",
                  last_upload_at=now - timedelta(days=3))
    giant = Creator(platform="youtube", channel_id="B", name="PlayStation", subscribers=17_000_000, language="en")
    s_mid, _ = core.score_contact(mid, vids, languages=["en"], has_email=True, manual=False, now=now)
    s_big, parts = core.score_contact(giant, vids[:1], languages=["en"], has_email=False, manual=False, now=now)
    assert s_mid > 70 > s_big
    assert any("brand" in label for label, _ in parts)


# ── keys, statuses, letters ──────────────────────────────────────────────────
@pytest.fixture
def pilot(session, project):
    p = project(name="Pilot Game", steam_app_id=1156500)
    g = OutreachGame(project_id=p.id, steam_app_id=1, name="Frostpunk", similarity=0.9, status="active")
    cr = Creator(platform="youtube", channel_id=f"UC{p.id}", name="Cozy Builder", handle="@cozy",
                 subscribers=12_000, language="en")
    session.add(g)
    session.add(cr)
    session.commit()
    session.add(CreatorVideo(creator_id=cr.id, game_id=g.id, video_id=f"v{p.id}", title="Frostpunk but zombies",
                             published_at=datetime(2026, 9, 1), views=5000))
    core.add_emails(cr, [f"cozy{p.id}@builder.tv"], "channel")  # unique: 3+ channels sharing one = sponsor
    session.add(cr)
    contact = OutreachContact(project_id=p.id, creator_id=cr.id)
    session.add(contact)
    session.commit()
    return p, cr, contact


def test_keys_import_assign_and_send(session, pilot):
    p, cr, contact = pilot
    added, known = core.import_keys(session, p.id, "AAAAA-BBBBB-CCCCC\naaaaa-bbbbb-ddddd junk\nAAAAA-BBBBB-CCCCC")
    assert (added, known) == (2, 0)
    assert core.import_keys(session, p.id, "AAAAA-BBBBB-CCCCC") == (0, 1)
    key = core.assign_key(session, contact)
    session.commit()
    assert key.key == "AAAAA-BBBBB-CCCCC" and key.status == "assigned"
    core.set_status(session, contact, "skipped")  # before sending → back to the pool
    assert session.get(SteamKey, key.id).status == "free" and contact.key_id is None
    core.assign_key(session, contact)
    core.set_status(session, contact, "sent")
    assert session.get(SteamKey, contact.key_id).status == "sent"
    assert contact.sent_at and contact.followup_at > contact.sent_at
    act = session.exec(select(Activity).where(Activity.project_id == p.id)).first()
    assert act.title == "Outreach: emailed 1 creator"
    core.set_status(session, contact, "skipped")  # after sending the key stays with the creator
    assert session.get(SteamKey, contact.key_id).status == "sent"


def test_letter_without_openai_uses_template_and_key(session, pilot):
    p, cr, contact = pilot
    core.save_settings(session, p.id, {"pitch": "A zombie city builder with a rebuilt core.", "signature": "Evgeny"})
    core.import_keys(session, p.id, "KEYAA-KEYBB-KEYCC")
    res = core.generate_letter(session, contact)
    assert any("OpenAI" in w for w in res["warnings"])
    assert "Frostpunk but zombies" in res["body"] and "KEYAA-KEYBB-KEYCC" in res["body"]
    assert "utm_medium=outreach" in res["body"] and "utm_content=cozy" in res["body"]
    assert contact.status == "drafted" and contact.email == f"cozy{p.id}@builder.tv"


def test_letter_with_openai_keeps_key_local(session, pilot, set_settings, monkeypatch):
    from app.services import openai_client
    p, cr, contact = pilot
    set_settings(openai_api_key="sk-test")
    core.import_keys(session, p.id, "LOCAL-KEYAA-KEYBB")
    sent = {}

    def fake(messages, **kw):
        sent["messages"] = messages
        return httpx.Response(200, json={"choices": [{"message": {"content": "SUBJECT: Pilot Game for you\n\nHi!\nSteam key: {KEY}\nBye"}}]}), "m"
    monkeypatch.setattr(openai_client, "chat_completion", fake)
    res = core.generate_letter(session, contact)
    assert res["subject"] == "Pilot Game for you" and "Steam key: LOCAL-KEYAA-KEYBB" in res["body"]
    assert "LOCAL-KEYAA-KEYBB" not in str(sent["messages"])  # the key never leaves the app


def test_shared_sponsor_emails_are_not_contacts(session):
    for i in range(3):
        c = Creator(platform="youtube", channel_id=f"S{i}", name=f"S{i}")
        core.add_emails(c, ["deals@sponsor.gg"], "video:x")
        session.add(c)
    session.commit()
    assert "deals@sponsor.gg" in core.shared_emails(session)
    c = session.exec(select(Creator).where(Creator.channel_id == "S0")).first()
    assert core.best_email(c, core.shared_emails(session)) is None


def test_csv_import_flexible_headers(session, project):
    p = project(name="CSV game")
    text = ("Channel Name;Channel URL;Published Email;Subscribers;Games\n"
            "Alpha;https://www.youtube.com/channel/UCalpha;alpha@mail.com;12.5K;Frostpunk\n"
            "Beta;https://www.twitch.tv/beta_tv;;3400;Kenshi\n"
            ";;;;\n")
    res = core.import_csv(session, p.id, text, "creatorscout.csv")
    assert res == {"added": 2, "updated": 0, "skipped": 1}
    alpha = session.exec(select(Creator).where(Creator.channel_id == "UCalpha")).first()
    assert alpha.subscribers == 12500 and core.emails_of(alpha)[0]["source"] == "csv:creatorscout.csv"
    beta = session.exec(select(Creator).where(Creator.platform == "twitch", Creator.channel_id == "beta_tv")).first()
    assert beta.url == "https://www.twitch.tv/beta_tv"
    contacts = session.exec(select(OutreachContact).where(OutreachContact.project_id == p.id)).all()
    assert len(contacts) == 2 and all(c.manual for c in contacts)


# ── YouTube discovery (mocked API) ───────────────────────────────────────────
def test_run_youtube_builds_contacts(session, project, monkeypatch):
    p = project(name="Disc game")
    g = OutreachGame(project_id=p.id, name="Frostpunk", status="active", similarity=0.9)
    session.add(g)
    session.commit()
    calls = []

    def fake_get(client, method, params):
        calls.append(method)
        if method == "search":
            return {"items": [
                {"id": {"videoId": "v1"}, "snippet": {"channelId": "UCa", "title": "Frostpunk &amp; chill",
                                                      "description": "", "publishedAt": "2026-08-01T10:00:00Z"}},
                {"id": {"videoId": "v2"}, "snippet": {"channelId": "UCb", "title": "Something else",
                                                      "description": "not this game", "publishedAt": "2026-08-01T10:00:00Z"}},
                {"id": {"videoId": "v3"}, "snippet": {"channelId": "UCc", "title": "Frostpunk trailer",
                                                      "description": "", "publishedAt": "2026-07-01T10:00:00Z"}},
            ]}
        if method == "channels":
            return {"items": [
                {"id": "UCa", "snippet": {"title": "Cozy Builder", "customUrl": "@cozy", "country": "GB",
                                          "description": "Business: disc@builder.tv https://linktr.ee/cozy"},
                 "statistics": {"subscriberCount": "12000", "viewCount": "100", "videoCount": "50"}},
                {"id": "UCc", "snippet": {"title": "Xbox", "description": ""},
                 "statistics": {"subscriberCount": "9000000"}},
            ]}
        if method == "videos":
            return {"items": [{"id": "v1", "snippet": {"title": "Frostpunk & chill", "defaultAudioLanguage": "en",
                                                       "description": "my discord https://discord.gg/x"},
                               "statistics": {"viewCount": "5000"}}]}
        raise AssertionError(method)

    monkeypatch.setattr(disc, "yt_get", fake_get)
    monkeypatch.setattr(disc, "rss_last_upload", lambda client, cid: datetime(2026, 10, 1))
    monkeypatch.setattr(disc, "run_links", lambda pid, **kw: "links skipped.")
    msg = disc.run_youtube(p.id)
    assert "1 game" in msg and "2 channels" in msg
    session.expire_all()
    cozy = session.exec(select(Creator).where(Creator.channel_id == "UCa")).first()
    assert cozy.language == "en"
    assert [(e["email"], e["source"]) for e in core.emails_of(cozy)] == [("disc@builder.tv", "channel")]
    contacts = {c.creator_id: c for c in session.exec(select(OutreachContact).where(OutreachContact.project_id == p.id)).all()}
    xbox = session.exec(select(Creator).where(Creator.channel_id == "UCc")).first()
    assert contacts[cozy.id].status == "found" and contacts[cozy.id].score > 60
    assert contacts[xbox.id].status == "skipped"  # brand channel
    assert session.exec(select(Creator).where(Creator.channel_id == "UCb")).first() is None  # title didn't match
    assert session.get(OutreachGame, g.id).videos_found == 2
    assert calls.count("search") == 1  # no nextPageToken → one page


# ── page and API ─────────────────────────────────────────────────────────────
def test_page_and_contact_api(client, session, pilot):
    p, cr, contact = pilot
    html = client.get(f"/outreach?project={p.id}").text
    assert "Creator outreach" in html and "Cozy Builder" in html and "Frostpunk" in html
    d = client.get(f"/api/outreach/contacts/{contact.id}").json()
    assert d["creator"]["name"] == "Cozy Builder" and d["email"] == f"cozy{p.id}@builder.tv"
    assert d["videos"][0]["title"] == "Frostpunk but zombies"
    r = client.post(f"/api/outreach/contacts/{contact.id}", data={"email": "not-an-email"})
    assert r.status_code == 400
    d = client.post(f"/api/outreach/contacts/{contact.id}/draft").json()
    assert d["status"] == "drafted" and d["body"] and d["gmail_url"].startswith("https://mail.google.com/")
    d = client.post(f"/api/outreach/contacts/{contact.id}", data={"status": "sent", "subject": "Edited"}).json()
    assert d["status"] == "sent" and d["subject"] == "Edited" and d["sent_at"]
    fu = client.post(f"/api/outreach/contacts/{contact.id}/followup").json()
    assert fu["subject"] == "Re: Edited" and "Cozy Builder" in fu["body"]
    assert client.get("/api/outreach/job").json()["search_limit"] == 100


def test_do_not_contact_blocks_drafts(client, session, pilot):
    p, cr, contact = pilot
    assert client.post(f"/api/outreach/creators/{cr.id}", data={"do_not_contact": 1}).json()["ok"]
    assert client.post(f"/api/outreach/contacts/{contact.id}/draft").status_code == 400


def test_games_api(client, session, project, monkeypatch):
    p = project(name="Games API")
    for i, sim in enumerate((0.9, 0.2, 0.6)):
        session.add(OutreachGame(project_id=p.id, name=f"S{i}", similarity=sim, status="suggested"))
    session.commit()
    assert client.post(f"/api/outreach/{p.id}/games/bulk", data={"action": "use_top", "top": 2}).json()["ok"]
    games = {g.name: g.status for g in session.exec(select(OutreachGame).where(OutreachGame.project_id == p.id)).all()}
    assert games == {"S0": "active", "S1": "suggested", "S2": "active"}
    g = session.exec(select(OutreachGame).where(OutreachGame.name == "S0")).first()
    g.searched_at = datetime(2026, 1, 1)
    session.add(g)
    session.commit()
    r = client.post(f"/api/outreach/games/{g.id}", data={"search_query": '"S0" gameplay'}).json()
    assert r["search_query"] == '"S0" gameplay'
    session.refresh(g)
    assert g.searched_at is None  # a new query is searched again
