"""YouTube setup checklist, the "YouTube is disconnected" banner and the connect flow."""
import httpx
import pytest
from fastapi.testclient import TestClient

from app.jobs import tasks
from app.services import tokens
from app.services import youtube_setup as ys
from app.services.settings import load_settings

CID = "123456789012-abcdef.apps.googleusercontent.com"
DAY = 86400
NO_VIDEOS = {"total": 0, "ok": 0, "failed": 0, "error": "", "checked_at": ""}
DEAD = {"error": "invalid_grant", "error_description": "Bad Request"}
_REAL_CLIENT = httpx.Client


@pytest.fixture(scope="module")
def client():
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def _route_httpx(monkeypatch, handler):
    monkeypatch.setattr(httpx, "Client",
                        lambda *a, **kw: _REAL_CLIENT(*a, transport=httpx.MockTransport(handler), **kw))


# ── token state ──────────────────────────────────────────────────────────────

def test_dead_refresh_token_is_remembered_once_and_cleared(set_settings, monkeypatch):
    set_settings(yt_client_id=CID, yt_client_secret="sec", yt_refresh_token="dead", yt_expires_at=0)
    _route_httpx(monkeypatch, lambda req: httpx.Response(400, json=DEAD))
    saves = []
    real_save = tokens.save_settings
    monkeypatch.setattr(tokens, "save_settings", lambda p: (saves.append(dict(p)), real_save(p))[1])

    for _ in range(2):  # the metrics schedule hits this every 30 minutes
        with pytest.raises(tokens.YouTubeAuthError):
            tokens.yt_access_token()
    cfg = load_settings()
    assert "invalid_grant" in cfg["yt_auth_error"] and cfg["yt_auth_error_at"] > 0
    assert len([s for s in saves if "yt_auth_error" in s]) == 1  # written once, not every run

    _route_httpx(monkeypatch, lambda req: httpx.Response(200, json={"access_token": "fresh", "expires_in": 3600}))
    assert tokens.yt_access_token() == "fresh"
    assert load_settings()["yt_auth_error"] == ""


def test_youtube_not_used_means_no_alert(client):
    with pytest.raises(tokens.YouTubeAuthError):
        tokens.yt_access_token()  # nothing configured
    assert load_settings()["yt_auth_error"] == ""
    assert client.get("/api/youtube/alert").text == ""


# ── checklist ────────────────────────────────────────────────────────────────

def test_console_links_preselect_the_project():
    links = ys.console_links(CID)
    assert links["audience"] == "https://console.cloud.google.com/auth/audience?project=123456789012"
    assert links["clients"] == "https://console.cloud.google.com/auth/clients?project=123456789012"
    assert ys.console_links("")["audience"] == "https://console.cloud.google.com/auth/audience"


def test_checklist_with_a_dead_token(client, set_settings, monkeypatch):
    set_settings(yt_client_id=CID, yt_client_secret="sec", yt_refresh_token="dead", yt_access_token="old",
                 yt_expires_at=0, yt_channel="<b>Chan</b>")
    _route_httpx(monkeypatch, lambda req: httpx.Response(400, json=DEAD))
    html = client.get("/api/youtube/setup").text

    assert "YouTube сейчас не работает" in html and "invalid_grant" in html
    for page in ("auth/audience", "auth/clients", "apis/library/youtube.googleapis.com"):
        assert f"https://console.cloud.google.com/{page}?project=123456789012" in html
    assert "http://127.0.0.1:8000/api/accounts/callback/youtube" in html  # redirect URI to copy
    assert 'href="/api/accounts/connect/youtube/go"' in html
    assert "Publish app" in html and "&lt;b&gt;Chan&lt;/b&gt;" in html
    assert "invalid_grant" in load_settings()["yt_auth_error"]  # the live check recorded it


def test_network_trouble_is_not_reported_as_a_dead_token(client, set_settings, monkeypatch):
    set_settings(yt_client_id=CID, yt_client_secret="sec", yt_refresh_token="r", yt_expires_at=0)

    def offline(req):
        raise httpx.ConnectError("offline")
    _route_httpx(monkeypatch, offline)
    html = client.get("/api/youtube/setup").text
    assert "проверить не удалось" in html
    assert load_settings()["yt_auth_error"] == ""


def test_state_when_everything_works():
    now = 1_800_000_000
    cfg = {"yt_client_id": CID, "yt_client_secret": "s", "yt_refresh_token": "r",
           "yt_connected_at": now - 10 * DAY, "yt_channel": "Sargonius Games (@sargoniusgames)"}
    metrics = {"total": 25, "ok": 25, "failed": 0, "error": "", "checked_at": "2026-09-26T10:00:00"}
    st = ys.setup_state(cfg, metrics, now=now)
    assert st["overall"]["status"] == "done" and "Sargonius Games" in st["overall"]["note"]
    assert {k: v["status"] for k, v in st["steps"].items()} == dict.fromkeys(
        ("api", "client", "publish", "connect", "stats"), "done")


def test_token_that_died_after_a_week_points_at_testing_mode():
    now = 1_800_000_000
    cfg = {"yt_client_id": CID, "yt_client_secret": "s", "yt_refresh_token": "r",
           "yt_connected_at": now - 8 * DAY, "yt_auth_error_at": now - DAY,
           "yt_auth_error": "yt_refresh_failed: invalid_grant Bad Request"}
    st = ys.setup_state(cfg, NO_VIDEOS, now=now)
    assert st["overall"]["status"] == "error"
    assert st["steps"]["publish"]["status"] == "error" and "Testing" in st["steps"]["publish"]["note"]
    assert st["steps"]["connect"]["status"] == "error"


def test_status_badge_asks_for_a_reconnect(client, set_settings):
    set_settings(yt_client_id=CID, yt_access_token="a", yt_refresh_token="r",
                 yt_auth_error="yt_refresh_failed: invalid_grant")
    assert client.get("/api/settings/status").json()["youtube"]["status"] == "not_configured"


# ── banner ───────────────────────────────────────────────────────────────────

def test_banner_only_while_youtube_is_broken(client, set_settings):
    set_settings(yt_refresh_token="r")
    assert client.get("/api/youtube/alert").text == ""
    set_settings(yt_auth_error="yt_refresh_failed: invalid_grant Bad Request", yt_auth_error_at=1_790_000_000)
    html = client.get("/api/youtube/alert").text
    assert "YouTube отключён" in html and "/accounts#yt_setup" in html


def test_every_page_loads_the_banner_and_accounts_the_checklist(client):
    html = client.get("/accounts").text
    assert 'id="yt_setup"' in html and 'hx-get="/api/youtube/setup"' in html
    assert 'hx-get="/api/youtube/alert"' in html  # base.html


# ── connect flow ─────────────────────────────────────────────────────────────

def test_connect_button_goes_straight_to_google(client, set_settings):
    r = client.get("/api/accounts/connect/youtube/go", follow_redirects=False)
    assert r.status_code == 400 and "Client ID" in r.text
    set_settings(yt_client_id=CID)
    r = client.get("/api/accounts/connect/youtube/go", follow_redirects=False)
    assert r.status_code == 302
    location = r.headers["location"]
    assert location.startswith("https://accounts.google.com/o/oauth2/v2/auth?") and "prompt=consent" in location
    assert f"state={load_settings()['oauth_state_youtube']}" in location


def test_reconnect_records_channel_and_warns_about_testing(client, set_settings, monkeypatch):
    set_settings(yt_client_id=CID, yt_client_secret="sec", oauth_state_youtube="st-2",
                 yt_auth_error="yt_refresh_failed: invalid_grant", yt_auth_error_at=1)
    monkeypatch.setattr(tasks.backfill_youtube_metrics, "schedule", lambda **kw: None)

    def google(req):
        if req.url.path == "/token":
            return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt", "expires_in": 3599,
                                             "refresh_token_expires_in": 604799})
        return httpx.Response(200, json={"items": [{"snippet": {"title": "Sargonius Games",
                                                                "customUrl": "@sargoniusgames"}}]})
    _route_httpx(monkeypatch, google)
    page = client.get("/api/accounts/callback/youtube", params={"code": "c", "state": "st-2"}).text
    assert "YouTube подключён" in page and "Sargonius Games (@sargoniusgames)" in page and "Testing" in page

    cfg = load_settings()
    assert cfg["yt_auth_error"] == "" and cfg["yt_channel"] == "Sargonius Games (@sargoniusgames)"
    assert cfg["yt_refresh_expires_at"] - cfg["yt_connected_at"] == 604799
    assert ys.alert(cfg, now=cfg["yt_connected_at"]) is None  # a week left: no banner yet
    assert ys.alert(cfg, now=cfg["yt_refresh_expires_at"] - DAY)["level"] == "warn"
    assert ys.setup_state(cfg, NO_VIDEOS)["steps"]["publish"]["status"] == "error"


def test_callback_errors_say_what_to_do(client, set_settings):
    r = client.get("/api/accounts/callback/youtube", params={"error": "access_denied"})
    assert r.status_code == 400 and "Publish app" in r.text and "/accounts#yt_setup" in r.text
    set_settings(oauth_state_youtube="expected")
    r = client.get("/api/accounts/callback/youtube", params={"code": "c", "state": "forged"})
    assert r.status_code == 400 and "Connect YouTube" in r.text
