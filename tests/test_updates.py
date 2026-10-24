"""Updates: versions, the GitHub check (a private repo, through the API, with a token), latest.json, the download and
its checksum, unpacking, and the banner / Settings section. GitHub is faked (httpx.MockTransport): no network."""
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from app import __version__
from app.services import updates

REPO = "me/IndiePulsePrivate"
TOKEN = "github_pat_test_0123456789"
STORAGE = "objects.githubusercontent.com"


@pytest.fixture(autouse=True)
def work_dir(tmp_path, monkeypatch):
    """The updates folder in tmp, and not a git checkout (the test run itself is one)."""
    monkeypatch.setattr(updates, "UPDATES", tmp_path / "updates")
    monkeypatch.setattr(updates, "is_git_checkout", lambda root=None: False)
    return tmp_path / "updates"


def release_zip(version="9.0.0", extra=None, folder="IndiePulse/"):
    files = {"app/__init__.py": f'__version__ = "{version}"\n', "app/main.py": "app = None\n",
             "IndiePulse.pyw": "", "IndiePulse.bat": "@echo off\r\n", "requirements.txt": "",
             "requirements.lock": "", **(extra or {})}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, text in files.items():
            z.writestr(name if name.startswith(("/", "..")) else folder + name, text)
    return buf.getvalue()


class FakeGitHub:
    """api.github.com for one repo, plus the storage host its asset downloads redirect to."""

    def __init__(self, *, private=True, version="9.0.0", zip_bytes=None, manifest=None, releases=True,
                 with_manifest=True, status=None):
        self.private, self.releases, self.status = private, releases, status
        self.zip = zip_bytes if zip_bytes is not None else release_zip(version)
        name = f"IndiePulse-{version}.zip"
        self.manifest = manifest or {
            "version": version, "tag": f"v{version}", "name": name,
            "url": f"https://github.com/{REPO}/releases/download/v{version}/{name}",
            "sha256": hashlib.sha256(self.zip).hexdigest(), "size": len(self.zip),
            "notes": "Calendar is faster\n- Shorts board fixed", "published_at": "2026-10-24T12:00:00Z"}
        assets = [{"id": 2, "name": name, "size": len(self.zip), "url": f"https://api.github.com/repos/{REPO}/releases/assets/2",
                   "browser_download_url": self.manifest["url"]}]
        if with_manifest:
            assets.append({"id": 1, "name": "latest.json", "url": f"https://api.github.com/repos/{REPO}/releases/assets/1",
                           "browser_download_url": "unused"})
        self.release = {"tag_name": f"v{version}", "html_url": f"https://github.com/{REPO}/releases/tag/v{version}",
                        "draft": False, "prerelease": False, "body": "body", "assets": assets}
        self.log = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.log.append(request)
        auth = request.headers.get("authorization")
        if request.url.host == STORAGE:
            assert auth is None, "the token must never reach the storage host"
            body = json.dumps(self.manifest).encode() if request.url.path.endswith("/1") else self.zip
            return httpx.Response(200, content=body)
        assert request.url.host == "api.github.com"
        if self.status:
            return httpx.Response(self.status[0], headers=self.status[1], json={"message": "no"})
        visible = not self.private or auth == f"Bearer {TOKEN}"
        path = request.url.path
        if path == f"/repos/{REPO}":
            return httpx.Response(200 if visible else 404, json={"full_name": REPO})
        if not visible:
            return httpx.Response(404, json={"message": "Not Found"})
        if path == f"/repos/{REPO}/releases/latest":
            return httpx.Response(200, json=self.release) if self.releases else httpx.Response(404, json={})
        if path.startswith(f"/repos/{REPO}/releases/assets/"):
            assert request.headers["accept"] == "application/octet-stream"
            asset = path.rsplit("/", 1)[1]
            return httpx.Response(302, headers={"location": f"https://{STORAGE}/blob/{asset}?sig=signed"})
        return httpx.Response(404)


@pytest.fixture
def github(monkeypatch, set_settings):
    def _make(token=TOKEN, **kw):
        gh = FakeGitHub(**kw)
        monkeypatch.setattr(updates, "_transport", httpx.MockTransport(gh.handler))
        set_settings(update_repo=REPO, update_token=token)
        return gh
    return _make


# ── versions ─────────────────────────────────────────────────────────────────
def test_version_order_dev_alpha_beta_rc_release():
    order = ["2.0.0-dev", "2.0.0-alpha", "2.0.0-beta.2", "2.0.0-rc.1", "2.0.0rc2", "2.0.0", "v2.0.1-dev", "2.0.1",
             "2.0.10", "2.1.0", "10.0.0"]
    keys = [updates.parse_version(v) for v in order]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)


def test_is_newer():
    assert updates.is_newer("2.0.0", "2.0.0-dev")
    assert updates.is_newer("2.0.0", "2.0.0-rc.3")
    assert not updates.is_newer("2.0.0-rc.1", "2.0.0")
    assert not updates.is_newer("2.0.0", "2.0.0")
    assert not updates.is_newer("2.0.0", "2.0.1-dev")  # main after the release isn't offered it back
    assert not updates.is_newer("banana", "1.0.0") and not updates.is_newer("2.0.0", "")
    with pytest.raises(ValueError):
        updates.parse_version("2.0")


def test_running_version_parses():
    updates.parse_version(__version__)


# ── the check ────────────────────────────────────────────────────────────────
def test_private_repo_with_a_token_goes_through_the_api(github):
    gh = github()
    state = updates.check()
    assert state["error"] == "" and state["checked_at"] and state["current"] == __version__
    latest = updates.available(state)
    assert latest["version"] == "9.0.0" and latest["tag"] == "v9.0.0"
    assert latest["sha256"] == hashlib.sha256(gh.zip).hexdigest()
    assert latest["asset_url"].endswith("/releases/assets/2")  # the API url, not browser_download_url
    assert latest["page"].endswith("/releases/tag/v9.0.0") and "Calendar is faster" in latest["notes"]
    api = [r for r in gh.log if r.url.host == "api.github.com"]
    assert api and all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in api)
    storage = [r for r in gh.log if r.url.host == STORAGE]
    assert len(storage) == 1 and "authorization" not in storage[0].headers
    assert updates.load_state()["latest"]["version"] == "9.0.0"  # kept for the banner


def test_private_repo_without_a_token_says_to_set_one(github):
    github(token="")
    state = updates.check()
    assert state["error_code"] == "need_token"
    assert "access token" in state["error"] and "Contents: read-only" in state["error"]
    assert updates.banner()["kind"] == "need_token"


def test_token_that_cant_see_the_repo(github):
    github(token="github_pat_other_one_xxxxxxxx")
    assert updates.check()["error_code"] == "bad_token"


def test_no_releases_yet_is_not_an_error(github):
    github(releases=False)
    state = updates.check()
    assert state["error"] == "" and state["latest"] is None and updates.banner() is None


def test_public_repo_needs_no_token(github):
    gh = github(token="", private=False)
    assert updates.available(updates.check())["version"] == "9.0.0"
    assert all("authorization" not in r.headers for r in gh.log)


@pytest.mark.parametrize("status,headers,code", [
    (401, {}, "bad_token"),
    (403, {"x-ratelimit-remaining": "0"}, "rate_limit"),
    (403, {}, "bad_token"),
    (500, {}, "error"),
])
def test_http_errors_are_explained(github, status, headers, code):
    github(status=(status, headers))
    assert updates.check()["error_code"] == code


def test_release_without_latest_json(github):
    github(with_manifest=False)
    state = updates.check()
    assert state["error_code"] == "bad_release" and "latest.json" in state["error"]


def test_network_error_keeps_the_last_known_release(github, monkeypatch):
    github()
    updates.check()

    def down(request):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(updates, "_transport", httpx.MockTransport(down))
    state = updates.check()
    assert state["error_code"] == "network"
    assert updates.available(state)["version"] == "9.0.0"


def test_bad_repo_name(set_settings):
    set_settings(update_repo="not a repo")
    assert updates.check()["error_code"] == "config"


# ── latest.json ──────────────────────────────────────────────────────────────
def _release(tag="v9.0.0", name="IndiePulse-9.0.0.zip"):
    return {"tag_name": tag, "assets": [{"name": name, "url": "https://api.github.com/x/assets/2", "size": 5}]}


def _manifest(**over):
    m = {"version": "9.0.0", "tag": "v9.0.0", "name": "IndiePulse-9.0.0.zip", "sha256": "ab" * 32, "notes": "n",
         "published_at": "2026-10-24T12:00:00Z", "url": "https://github.com/x/releases/download/v9.0.0/IndiePulse-9.0.0.zip"}
    m.update(over)
    return json.dumps(m).encode()


def test_manifest_parsed():
    m = updates.parse_manifest(_manifest(), _release())
    assert (m["version"], m["name"], m["sha256"], m["size"]) == ("9.0.0", "IndiePulse-9.0.0.zip", "ab" * 32, 5)


def test_manifest_name_falls_back_to_the_url():
    assert updates.parse_manifest(_manifest(name=""), _release())["name"] == "IndiePulse-9.0.0.zip"


@pytest.mark.parametrize("data,release", [
    (b"{not json", _release()),
    (b"[1, 2]", _release()),
    (_manifest(version="nine"), _release()),
    (_manifest(tag="v9.0.1"), _release()),                       # tag doesn't go with the version
    (_manifest(), _release(tag="v8.0.0")),                       # attached to another release
    (_manifest(sha256="xyz"), _release()),
    (_manifest(name="../../evil.zip"), _release()),
    (_manifest(), _release(name="IndiePulse-other.zip")),        # the zip it names isn't attached
])
def test_manifest_that_doesnt_add_up_is_rejected(data, release):
    with pytest.raises(updates.UpdateError):
        updates.parse_manifest(data, release)


@pytest.mark.skipif(__import__("shutil").which("git") is None, reason="no git")
def test_build_release_makes_a_manifest_the_updater_accepts(tmp_path):
    """tools/build_release.py's latest.json, read back by parse_manifest."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "tools"))
    import build_release as br
    repo = tmp_path / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "app/__init__.py").write_text('__version__ = "9.1.0"\n')
    (repo / "IndiePulse.pyw").write_text("")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    m = br.build("v9.1.0", tmp_path / "dist", repo=REPO, notes="x", root=repo)
    release = {"tag_name": "v9.1.0", "assets": [{"name": "IndiePulse-9.1.0.zip", "url": "https://api.github.com/a/2"}]}
    assert updates.parse_manifest(json.dumps(m).encode(), release)["sha256"] == m["sha256"]


# ── download, checksum, unpack ───────────────────────────────────────────────
def test_download_checks_the_sha_and_unpacks(github, work_dir):
    gh = github()
    root = updates.download(updates.available(updates.check()), TOKEN)
    assert root == work_dir / "staging" / "IndiePulse"
    assert (root / "app" / "__init__.py").read_text() == '__version__ = "9.0.0"\n'
    assert (work_dir / "IndiePulse-9.0.0.zip").read_bytes() == gh.zip
    assert json.loads((work_dir / "ready.json").read_text())["version"] == "9.0.0"
    zip_requests = [r for r in gh.log if r.url.path.endswith("/assets/2") or r.url.path.endswith("/blob/2")]
    assert [r.url.host for r in zip_requests] == ["api.github.com", STORAGE]
    assert "authorization" not in zip_requests[1].headers


def test_checksum_mismatch_is_rejected(github, work_dir):
    gh = github()
    latest = updates.available(updates.check())
    gh.zip = gh.zip + b"tampered"
    with pytest.raises(updates.UpdateError) as exc:
        updates.download(latest, TOKEN)
    assert exc.value.code == "checksum"
    assert not list(work_dir.glob("*.zip")) and not list(work_dir.glob("*.part"))
    assert not (work_dir / "ready.json").exists()


@pytest.mark.parametrize("zip_bytes,match", [
    (release_zip(extra={"../evil.txt": "x"}), "unsafe path"),
    (release_zip(extra={"/abs.txt": "x"}), "unsafe path"),
    (release_zip(version="8.0.0"), "holds IndiePulse 8.0.0"),
    (release_zip(extra={".venv/pyvenv.cfg": "x"}), "never replaces"),
    (release_zip(extra={"indiepulse.db": "x"}), "never replaces"),
    (b"not a zip at all", "isn't a valid zip"),
])
def test_unpack_refuses_a_bad_zip(work_dir, zip_bytes, match):
    work_dir.mkdir(parents=True)
    z = work_dir / "IndiePulse-9.0.0.zip"
    z.write_bytes(zip_bytes)
    with pytest.raises(updates.UpdateError, match=match):
        updates.unpack(z, "9.0.0")


def test_unpack_takes_a_zip_without_the_folder(work_dir):
    work_dir.mkdir(parents=True)
    z = work_dir / "IndiePulse-9.0.0.zip"
    z.write_bytes(release_zip(folder=""))
    assert updates.unpack(z, "9.0.0") == work_dir / "staging"


# ── settings ─────────────────────────────────────────────────────────────────
def test_settings_defaults_and_redaction(set_settings):
    from app.core.redact import redact
    c = updates.config()
    assert c == {"mode": "notify", "repo": "Sargonius/IndiePulseNew", "token": ""}
    set_settings(update_token=TOKEN, update_mode="nonsense")
    assert updates.config()["mode"] == "notify"
    assert TOKEN not in redact(f"GET with {TOKEN}")


def test_settings_section_saves_mode_repo_token(client, settings_file):
    r = client.post("/api/updates/save", data={"update_mode": "auto",
                                               "update_repo": "https://github.com/me/Other.git",
                                               "update_token": TOKEN})
    assert r.status_code == 200 and "Saved." in r.text
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert (saved["update_mode"], saved["update_repo"], saved["update_token"]) == ("auto", "me/Other", TOKEN)
    assert TOKEN not in r.text and "•••6789" in r.text
    client.post("/api/updates/save", data={"update_mode": "auto"})  # an empty token field keeps the token
    assert json.loads(settings_file.read_text(encoding="utf-8"))["update_token"] == TOKEN
    client.post("/api/updates/save", data={"clear_token": "1"})
    assert json.loads(settings_file.read_text(encoding="utf-8"))["update_token"] == ""
    assert "owner/name" in client.post("/api/updates/save", data={"update_repo": "nope"}).text


def test_settings_page_loads_the_section(client):
    assert 'hx-get="/api/updates/panel"' in client.get("/accounts").text
    panel = client.get("/api/updates/panel").text
    assert 'id="section_updates"' in panel and __version__ in panel and "Contents: Read-only" in panel


def test_check_now_from_settings(client, github):
    github()
    r = client.post("/api/updates/check")
    assert "IndiePulse 9.0.0 is available." in r.text


def test_check_now_without_token_says_so(client, github):
    github(token="")
    assert "set an access token" in client.post("/api/updates/check").text


# ── banner ───────────────────────────────────────────────────────────────────
def _available(version="9.0.0"):
    updates.save_state({"checked_at": 1, "latest": {"version": version, "tag": f"v{version}", "notes": "<b>new</b> stuff",
                                                    "page": "https://github.com/x/releases/tag/v9.0.0"}})


def test_every_page_loads_the_banner(client):
    assert 'hx-get="/api/updates/banner"' in client.get("/modules").text


def test_banner_offers_an_available_version(client, monkeypatch):
    monkeypatch.setenv("INDIEPULSE_SUPERVISED", "1")
    _available()
    html = client.get("/api/updates/banner").text
    assert "IndiePulse v9.0.0 is available" in html and "What's new" in html
    assert "hx-post='/api/updates/install?view=banner'" in html
    assert "&lt;b&gt;new&lt;/b&gt; stuff" in html  # notes escaped


def test_banner_without_the_tray_says_where_to_install(client, monkeypatch):
    monkeypatch.delenv("INDIEPULSE_SUPERVISED", raising=False)
    _available()
    html = client.get("/api/updates/banner").text
    assert "is available" in html and "install?view=banner" not in html


def test_banner_empty_when_up_to_date_or_off(client, set_settings):
    assert client.get("/api/updates/banner").text == ""
    _available(version="0.0.1")
    assert client.get("/api/updates/banner").text == ""
    _available()
    set_settings(update_mode="off")
    assert client.get("/api/updates/banner").text == ""


def test_git_checkout_banner_says_git_pull_and_install_is_refused(client, monkeypatch):
    monkeypatch.setattr(updates, "is_git_checkout", lambda root=None: True)
    monkeypatch.setenv("INDIEPULSE_SUPERVISED", "1")
    _available()
    html = client.get("/api/updates/banner").text
    assert "git checkout" in html and "git pull" in html and "install?view" not in html
    r = client.post("/api/updates/install")
    assert "git pull" in r.text and not updates.install_request_path().exists()


def test_install_asks_the_tray(client, monkeypatch):
    monkeypatch.setenv("INDIEPULSE_SUPERVISED", "1")
    _available()
    html = client.post("/api/updates/install").text
    assert updates.install_request_path().read_text() == "9.0.0"
    assert updates.progress()["phase"] == "requested"
    assert "every 5s" in html and "Starting the update" in html


def test_install_needs_the_tray(client, monkeypatch):
    monkeypatch.delenv("INDIEPULSE_SUPERVISED", raising=False)
    _available()
    assert "tray" in client.post("/api/updates/install").text
    assert not updates.install_request_path().exists()


def test_banner_follows_an_install_then_says_how_it_went(client, monkeypatch):
    import time
    updates.set_phase("waiting", "9.0.0", "Waiting for 1 post being published to finish")
    assert "every 5s" in client.get("/api/updates/banner").text
    updates.set_phase("rolled_back", "9.0.0", "IndiePulse 9.0.0 didn't start, so 2.0.0 is back.")
    html = client.get("/api/updates/banner").text
    assert "so 2.0.0 is back" in html and "/api/updates/dismiss" in html
    assert client.post("/api/updates/dismiss").text == ""
    updates.set_phase("installed", __version__, f"IndiePulse updated to {__version__}.")
    assert "updated to" in client.get("/api/updates/banner").text
    updates.set_phase("downloading", "9.0.0", "Downloading")
    progress = json.loads((updates.UPDATES / "progress.json").read_text())
    progress["at"] = time.time() - 3600  # its tray is gone
    (updates.UPDATES / "progress.json").write_text(json.dumps(progress))
    assert client.get("/api/updates/banner").text == ""


def test_busy_reports_a_publish_in_progress(client, make_post, session):
    from app.models.models import Post, PostStatusEnum
    post_id, _ = make_post("tg", status=PostStatusEnum.running)
    try:
        assert "being published" in client.get("/api/updates/busy").json()["busy"]
    finally:  # one DB for the whole run: don't leave a "running" post for the other tests
        post = session.get(Post, post_id)
        post.status = PostStatusEnum.done
        session.add(post)
        session.commit()
    assert updates.busy() == "" or "job" in updates.busy() or "post" in updates.busy()
