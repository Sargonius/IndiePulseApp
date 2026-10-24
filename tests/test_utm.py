"""UTM links: slugs, URL building, the {steam} placeholder, API and the /utm page."""
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlmodel import select

from app.models.models import Goal, Project, UtmLink
from app.services import utm


@pytest.fixture
def project(session):
    def _make(name="Game", **kw):
        p = Project(user_id=1, name=name, **kw)
        session.add(p)
        session.commit()
        session.refresh(p)
        return p
    return _make


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def _links(session, pid):
    session.expire_all()
    return session.exec(select(UtmLink).where(UtmLink.project_id == pid)).all()


# ── slug ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw, expected", [
    ("Patch Oct", "patch_oct"),
    ("  patch   oct  ", "patch_oct"),
    ("Патч Октябрь!", "patch_oktyabr"),
    ("Щука и Ёж", "shchuka_i_ezh"),
    ("Next-Fest 2027", "next-fest_2027"),
    ("a - b", "a_b"),
    ("a__b--c", "a_b-c"),
    ("Café déjà vu", "cafe_deja_vu"),
    ("パッチ", ""),
    ("パッチ v2", "v2"),
    ("!!!", ""),
    ("", ""),
    (None, ""),
])
def test_slug(raw, expected):
    assert utm.slug(raw) == expected


def test_slug_is_capped_and_trimmed():
    s = utm.slug("word " * 30)
    assert len(s) <= utm.SLUG_MAX
    assert not s.endswith("_") and not s.startswith("_")
    assert utm.slug("x" * 100) == "x" * utm.SLUG_MAX


# ── build_url / placeholders ─────────────────────────────────────────────────

def test_build_url_steam_and_empty_params_omitted():
    url = utm.build_url(utm.steam_base_url(123), "tiktok", "bio", "patch_oct")
    assert url == "https://store.steampowered.com/app/123/?utm_source=tiktok&utm_medium=bio&utm_campaign=patch_oct"
    assert "utm_content" not in url and "utm_term" not in url


def test_build_url_keeps_existing_query_and_encodes():
    url = utm.build_url("https://example.itch.io/game?ref=x&utm_source=old#top", "reddit", "post", "a b&c",
                        content="v/2", term="t")
    parts = urlsplit(url)
    q = parse_qs(parts.query)
    assert q == {"ref": ["x"], "utm_source": ["reddit"], "utm_medium": ["post"], "utm_campaign": ["a b&c"],
                 "utm_term": ["t"], "utm_content": ["v/2"]}
    assert "a+b%26c" in parts.query and "v%2F2" in parts.query
    assert parts.fragment == "top"


def test_check_base_url():
    assert utm.check_base_url(" https://x.itch.io/g ") == "https://x.itch.io/g"
    for bad in ("x.itch.io", "javascript:alert(1)", "https://", "ftp://x.com/a"):
        with pytest.raises(ValueError):
            utm.check_base_url(bad)


def test_expand_placeholders():
    text = "Патч вышел! {steam} #indiedev"
    out = utm.expand_placeholders(text, appid=480, platform="tg", medium="post", campaign="Патч Октябрь")
    assert out == ("Патч вышел! https://store.steampowered.com/app/480/"
                   "?utm_source=telegram&utm_medium=post&utm_campaign=patch_oktyabr #indiedev")
    assert "utm_source=x&" in utm.expand_placeholders("{steam}", appid=1, platform="x", medium="post", campaign="c")
    # no token / no appid → unchanged
    assert utm.expand_placeholders("no link", appid=480, platform="x", medium="post", campaign="c") == "no link"
    assert utm.expand_placeholders("{steam}", appid=None, platform="x", medium="post", campaign="c") == "{steam}"


def test_registry_is_consistent():
    for key, src in utm.SOURCES.items():
        assert src["label"] and src["placements"]
        for p in src["placements"]:
            assert p["key"] in utm.PLACEMENT_LABELS and p["hint"]
    for src, med in utm.CAMPAIGN_SET:
        assert utm.placement(src, med) is not None
    assert utm.placement("tiktok", "bio")["clickable"] is True
    assert utm.placement("youtube", "shorts")["clickable"] is False


# ── API and page ─────────────────────────────────────────────────────────────

def test_create_duplicate_delete(client, session, project):
    p = project("UtmGame", steam_app_id=480)
    form = {"project_id": str(p.id), "source": "tiktok", "medium": "bio", "campaign": "Patch Oct"}
    r = client.post("/api/utm/links", data=form, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/utm?")
    r = client.post("/api/utm/links", data=form, follow_redirects=False)
    assert r.status_code == 303
    links = _links(session, p.id)
    assert len(links) == 1 and links[0].campaign == "patch_oct" and links[0].content == ""

    page = client.get(f"/utm?project={p.id}")
    assert page.status_code == 200
    assert "https://store.steampowered.com/app/480/?utm_source=tiktok&amp;utm_medium=bio&amp;utm_campaign=patch_oct" in page.text
    assert "utmtrafficstats/480" in page.text

    r = client.post(f"/api/utm/links/{links[0].id}/delete", data={"next": "https://evil.example/"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/utm"
    assert _links(session, p.id) == []


def test_create_validation(client, project):
    p = project("NoAppId")
    base = {"project_id": str(p.id), "source": "x", "medium": "post", "campaign": "c1"}
    r = client.post("/api/utm/links", data=base, follow_redirects=False)
    assert r.status_code == 400 and "Steam App ID" in r.json()["detail"]
    r = client.post("/api/utm/links", data={**base, "base_url": "not a url"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/api/utm/links", data={**base, "medium": "bio_nope"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/api/utm/links", data={**base, "campaign": "パッチ"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.post("/api/utm/set", data={"project_id": str(p.id), "campaign": "c1"}, follow_redirects=False)
    assert r.status_code == 400
    r = client.get("/api/utm/build", params=base)
    assert r.status_code == 400 and r.json()["ok"] is False


def test_custom_base_url_for_project_without_appid(client, session, project):
    p = project("ItchOnly")
    r = client.post("/api/utm/links", data={"project_id": str(p.id), "source": "itch", "medium": "description",
                                            "campaign": "demo", "base_url": "https://me.itch.io/game"},
                    follow_redirects=False)
    assert r.status_code == 303
    [link] = _links(session, p.id)
    assert link.base_url == "https://me.itch.io/game"
    page = client.get("/utm")
    assert "https://me.itch.io/game?utm_source=itch&amp;utm_medium=description&amp;utm_campaign=demo" in page.text


def test_build_endpoint(client, project):
    p = project("Build", steam_app_id=777)
    r = client.get("/api/utm/build", params={"project_id": p.id, "source": "youtube", "medium": "shorts",
                                            "campaign": "Патч", "content": "Trailer V2"})
    data = r.json()
    assert r.status_code == 200 and data["ok"]
    assert data["url"] == ("https://store.steampowered.com/app/777/?utm_source=youtube&utm_medium=shorts"
                           "&utm_campaign=patch&utm_content=trailer_v2")
    assert data["clickable"] is False and data["hint"]


def test_campaign_set(client, session, project):
    p = project("SetGame", steam_app_id=999)
    session.add(Goal(title="Патч октябрь: +50", project_id=p.id, metric="steam_wishlist_adds", target=50,
                     start_day="2026-10-01"))
    session.commit()
    data = {"project_id": str(p.id), "campaign": "patch_oct"}
    r = client.post("/api/utm/set", data=data, follow_redirects=False)
    assert r.status_code == 303 and "set=1" in r.headers["location"]
    client.post("/api/utm/set", data=data, follow_redirects=False)  # twice → still one set
    links = _links(session, p.id)
    assert sorted((ln.source, ln.medium) for ln in links) == sorted(utm.CAMPAIGN_SET)

    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert 'id="utm-set"' in page.text
    assert "utm_source=reddit&amp;utm_medium=post&amp;utm_campaign=patch_oct" in page.text
    # goal titles become campaign suggestions
    assert "patch_oktyabr_50" in page.text


def test_navigation_links(client, project):
    project("NavGame", steam_app_id=1)
    assert 'href="/utm"' in client.get("/guide").text
    assert "/utm?project=" in client.get("/").text


# ── {steam} at publish time ──────────────────────────────────────────────────

def test_publish_brief_gets_platform_link_and_keeps_stored_token(session, project):
    from app.models.models import Brief, GoalLink, Post
    from app.platforms.common import brief_for_publish
    p = project(steam_app_id=31337)
    goal = Goal(title="Patch Oct", project_id=p.id, metric="steam_visits", target=100, start_day="2026-10-01")
    brief = Brief(user_id=1, type="news", text_en="Patch is out {steam}", text_ru="", project_id=p.id)
    session.add(goal)
    session.add(brief)
    session.commit()
    session.add(GoalLink(goal_id=goal.id, brief_id=brief.id))
    session.commit()

    urls = {}
    for platform in ("x", "tg"):
        post = Post(user_id=1, brief_id=brief.id, platform=platform)
        text = brief_for_publish(session, post).text_en
        urls[platform] = parse_qs(urlsplit(text.split()[-1]).query)
    assert urls["x"] == {"utm_source": ["x"], "utm_medium": ["post"], "utm_campaign": ["patch_oct"]}
    assert urls["tg"]["utm_source"] == ["telegram"]
    session.commit()
    session.expire_all()
    assert session.get(Brief, brief.id).text_en == "Patch is out {steam}"


def test_publish_brief_without_app_id_drops_the_token(session, project):
    from app.models.models import Brief, Post
    from app.platforms.common import brief_for_publish
    p = project()
    brief = Brief(user_id=1, type="news", text_en="Patch is out {steam}", project_id=p.id)
    plain = Brief(user_id=1, type="news", text_en="No link here")
    session.add(brief)
    session.add(plain)
    session.commit()
    assert brief_for_publish(session, Post(user_id=1, brief_id=brief.id, platform="x")).text_en == "Patch is out"
    assert brief_for_publish(session, Post(user_id=1, brief_id=plain.id, platform="x")) is plain
