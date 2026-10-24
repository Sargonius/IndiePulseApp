"""Updates from GitHub Releases: is there a newer IndiePulse, and getting it ready to install.

A release (.github/workflows/release.yml, from a v* tag) has two assets: IndiePulse-<version>.zip — IndiePulse.exe,
README.md, LICENSE and lib\\ under one IndiePulse/ folder — and latest.json = {version, tag, name, url, sha256, size,
notes, published_at}. Everything goes through the API: the release (GET /repos/{repo}/releases/latest), then each
asset — latest.json, the zip — by its API url with Accept: application/octet-stream. GitHub answers that with a
redirect to its storage host; the redirect is followed here, by hand. The repo is public, so no token is needed; a
private fork sets `update_token` (a fine-grained token: that repository, Contents: read-only), which goes to the API
host only, never along the redirect (browser_download_url doesn't work for a private repo at all).

Who does what:
- checking: the tray, 30 s after start and every 6 h (app/desktop/updater.py — the process that lives as long as
  IndiePulse does, and the one that installs), and Settings → Updates «Check now» from the server. Both write
  state.json; the banner and Settings only read it, so page loads never touch the network;
- downloading, checking the sha256 against latest.json, unpacking: the tray, while the app keeps running;
- installing (stop everything, swap the code, start, roll back if it doesn't come up): app/desktop/apply_update.py,
  which also writes progress.json (the tray writes it before that: downloading, waiting for a publish to finish).

A checkout (the developer's copy: HOME/.git, or any copy without a release's lib\\) is never updated in place, and
doesn't check by itself: git pull.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import zipfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

import httpx

from app import __version__
from app.core.paths import DATA, HOME
from app.desktop.apply_update import find_release_root, read_json, release_items, set_progress, version_of, \
	write_json

MODES = ("off", "notify", "auto")
API = "https://api.github.com"
FIRST_CHECK_AFTER = 30   # s after the tray starts
CHECK_EVERY = 6 * 3600
MAX_DOWNLOAD = 300 * 1024 * 1024
TOKEN_HELP = "a fine-grained token for this repository with Contents: read-only"
ACTIVE = ("requested", "downloading", "waiting", "installing")  # progress.json phases while an install is under way
DONE = ("installed", "rolled_back", "failed")
_STALE = 15 * 60        # an ACTIVE phase not refreshed for this long is forgotten (its tray is gone)
_SHOW_DONE = 3 * 86400  # how long the banner says how an install went (unless dismissed)
_REPO_RX = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA_RX = re.compile(r"[0-9a-f]{64}")
_ZIP_RX = re.compile(r"IndiePulse-[0-9A-Za-z.+-]+\.zip")
_VERSION_RX = re.compile(r"v?(\d+)\.(\d+)\.(\d+)(?:[-.]?(dev|alpha|a|beta|b|rc)[.-]?(\d*))?", re.I)
_STAGES = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3}
_RELEASED = 4


def _work_dir() -> Path:
	"""DATA/updates, or .updates in IndiePulse's folder when the data is on another drive: the swap moves lib\\ and the
	rest between this folder and HOME, and only within one drive is a move a rename — at once, nothing copied."""
	if os.path.splitdrive(str(DATA))[0].lower() != os.path.splitdrive(str(HOME))[0].lower():
		return HOME / ".updates"
	return DATA / "updates"


UPDATES = _work_dir()
_transport: Optional[httpx.BaseTransport] = None  # tests put an httpx.MockTransport here


class UpdateError(Exception):
	"""Something to show the user as is (Settings → Updates, a tray balloon)."""

	def __init__(self, text: str, code: str = "error"):
		super().__init__(text)
		self.code = code


# ── versions and settings ────────────────────────────────────────────────────
def parse_version(text: str) -> tuple:
	"""'2.0.0' → a tuple that sorts right: 2.0.0-dev < 2.0.0-alpha < 2.0.0-beta < 2.0.0-rc.1 < 2.0.0 < 2.0.1-dev
	(PEP 440's order; also takes 'v2.0.0', '2.0.0rc1'). ValueError for anything else."""
	m = _VERSION_RX.fullmatch((text or "").strip())
	if not m:
		raise ValueError(f"not a version: {text!r}")
	major, minor, patch, stage, number = m.groups()
	return int(major), int(minor), int(patch), _STAGES[stage.lower()] if stage else _RELEASED, int(number or 0)


def is_newer(candidate: str, current: str = __version__) -> bool:
	try:
		return parse_version(candidate) > parse_version(current)
	except ValueError:
		return False


def config(cfg: Optional[dict] = None) -> dict:
	if cfg is None:
		from app.services.settings import load_settings
		cfg = load_settings()
	mode = str(cfg.get("update_mode") or "notify").strip().lower()
	return {"mode": mode if mode in MODES else "notify",
			"repo": str(cfg.get("update_repo") or "").strip().strip("/"),
			"token": str(cfg.get("update_token") or "").strip()}


def valid_repo(name: str) -> bool:
	return bool(_REPO_RX.fullmatch(name or ""))


def is_git_checkout(root: Path = HOME) -> bool:
	"""Not a release install: a git checkout (.git — a folder, or a file in a git worktree), or a copy of the source
	without the release's IndiePulse.exe + lib\\, which a release zip can't be swapped into."""
	root = Path(root)
	return (root / ".git").exists() or not (root / "lib" / "app" / "__init__.py").is_file()


# ── state files ──────────────────────────────────────────────────────────────
def _state_path() -> Path:
	return UPDATES / "state.json"


def install_request_path() -> Path:
	return UPDATES / "install.request"


def load_state() -> dict:
	return read_json(_state_path())


def save_state(state: dict) -> None:
	write_json(_state_path(), state)


def progress() -> dict:
	"""What an install is doing or how the last one went ({} = nothing); an ACTIVE phase nobody refreshed is dropped."""
	p = read_json(UPDATES / "progress.json")
	if p.get("phase") in ACTIVE and time.time() - (p.get("at") or 0) > _STALE:
		return {}
	return p


def set_phase(phase: str, version: str = "", text: str = "", **extra) -> dict:
	return set_progress(UPDATES, phase, version, text, **extra)


def mark_progress(**fields) -> None:
	"""Add flags to the current progress.json (seen by the web UI, reported by the tray)."""
	p = read_json(UPDATES / "progress.json")
	if p:
		write_json(UPDATES / "progress.json", {**p, **fields})


def available(state: Optional[dict] = None) -> Optional[dict]:
	"""The latest release, when it is newer than the running code."""
	latest = (load_state() if state is None else state).get("latest")
	return latest if isinstance(latest, dict) and is_newer(str(latest.get("version") or "")) else None


# ── GitHub ───────────────────────────────────────────────────────────────────
def _client() -> httpx.Client:
	return httpx.Client(timeout=httpx.Timeout(20.0, read=60.0), transport=_transport, follow_redirects=False,
						headers={"User-Agent": f"IndiePulse/{__version__}", "X-GitHub-Api-Version": "2022-11-28"})


def _get(client: httpx.Client, url: str, token: str, *, accept: str = "application/vnd.github+json",
		 stream: bool = False) -> httpx.Response:
	"""GET, following redirects by hand: the token goes to the API host only — an asset download is redirected to
	GitHub's storage host, which gets a signed URL instead and must never see the token."""
	api_host = httpx.URL(API).host
	for _ in range(5):
		headers = {"Accept": accept}
		if token and httpx.URL(url).host == api_host and url.startswith("https://"):
			headers["Authorization"] = f"Bearer {token}"
		r = client.send(client.build_request("GET", url, headers=headers), stream=stream)
		if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
			r.close()
			url = str(r.url.join(r.headers["location"]))
			continue
		return r
	raise UpdateError("GitHub redirected too many times")


def _http_error(r: httpx.Response, repo: str, token: str, what: str) -> UpdateError:
	if r.status_code == 401:
		return UpdateError(f"GitHub didn't accept the access token (expired or revoked?): make a new one — "
						   f"{TOKEN_HELP}.", "bad_token")
	if r.status_code in (403, 429) and r.headers.get("x-ratelimit-remaining") == "0":
		who = "this token" if token else "checks without a token"
		return UpdateError(f"GitHub's hourly request limit for {who} is used up; the next check will do.", "rate_limit")
	if r.status_code == 403:
		return UpdateError(f"The access token can't read {repo}: it needs {TOKEN_HELP}.", "bad_token")
	return UpdateError(f"GitHub answered HTTP {r.status_code} for {what}.")


def _asset(release: dict, name: str) -> Optional[dict]:
	return next((a for a in release.get("assets") or [] if isinstance(a, dict) and a.get("name") == name
				 and a.get("url")), None)


def parse_manifest(data: bytes, release: dict) -> dict:
	"""latest.json of `release` (the API's release object) → what the updater keeps; UpdateError when it doesn't
	add up (the version, the tag, the sha256, the zip it names among the release's assets)."""
	try:
		m = json.loads(data.decode("utf-8-sig"))
	except (UnicodeDecodeError, ValueError):
		raise UpdateError("The release's latest.json isn't valid JSON.", "bad_release")
	if not isinstance(m, dict):
		raise UpdateError("The release's latest.json isn't an object.", "bad_release")
	version, tag = str(m.get("version") or ""), str(m.get("tag") or "")
	try:
		parse_version(version)
	except ValueError:
		raise UpdateError(f"latest.json has no valid version ({version!r}).", "bad_release")
	if tag != f"v{version}":
		raise UpdateError(f"latest.json: tag {tag!r} doesn't go with version {version}.", "bad_release")
	if release.get("tag_name") != tag:
		raise UpdateError(f"latest.json says {tag}, but it is attached to release {release.get('tag_name')}.",
						  "bad_release")
	sha = str(m.get("sha256") or "").strip().lower()
	if not _SHA_RX.fullmatch(sha):
		raise UpdateError("latest.json has no valid sha256.", "bad_release")
	name = str(m.get("name") or "") or os.path.basename(urlsplit(str(m.get("url") or "")).path)
	if not _ZIP_RX.fullmatch(name):
		raise UpdateError(f"latest.json names no IndiePulse zip ({name!r}).", "bad_release")
	zip_asset = _asset(release, name)
	if zip_asset is None:
		raise UpdateError(f"Release {tag} has no {name} attached.", "bad_release")
	return {
		"version": version, "tag": tag, "name": name, "sha256": sha,
		"size": int(m.get("size") or zip_asset.get("size") or 0),
		"notes": str(m.get("notes") or release.get("body") or ""),
		"published_at": str(m.get("published_at") or release.get("published_at") or ""),
		"page": str(release.get("html_url") or ""),
		"asset_url": str(zip_asset["url"]),
	}


def fetch_latest(repo: str, token: str) -> Optional[dict]:
	"""The latest release's manifest (None: the repo has no releases yet); UpdateError explains a problem."""
	if not valid_repo(repo):
		raise UpdateError("Set the repository as owner/name in Settings → Updates.", "config")
	with _client() as client:
		r = _get(client, f"{API}/repos/{repo}/releases/latest", token)
		if r.status_code == 404:
			# no releases yet — or a private repo this request can't see: GitHub says 404 to both
			if _get(client, f"{API}/repos/{repo}", token).status_code == 200:
				return None
			if not token:
				raise UpdateError(f"{repo} is private (or doesn't exist): set an access token in Settings → Updates "
								  f"— {TOKEN_HELP}.", "need_token")
			raise UpdateError(f"{repo} isn't visible with this token: check the name, and that the token is "
							  f"{TOKEN_HELP}.", "bad_token")
		if r.status_code != 200:
			raise _http_error(r, repo, token, "the latest release")
		release = r.json()
		tag = release.get("tag_name") or "?"
		asset = _asset(release, "latest.json")
		if asset is None:
			raise UpdateError(f"Release {tag} has no latest.json: it wasn't made by the release workflow.",
							  "bad_release")
		mr = _get(client, asset["url"], token, accept="application/octet-stream")
		if mr.status_code != 200:
			raise _http_error(mr, repo, token, f"latest.json of {tag}")
		return parse_manifest(mr.content, release)


def check(cfg: Optional[dict] = None) -> dict:
	"""Ask GitHub; the answer — or why there is none — goes to state.json. Returns the new state. After an error the
	last known release stays (a network blip doesn't hide an available update)."""
	c = config(cfg)
	state = load_state()
	state.update(checked_at=int(time.time()), current=__version__, repo=c["repo"], error="", error_code="")
	try:
		state["latest"] = fetch_latest(c["repo"], c["token"])
	except UpdateError as exc:
		state.update(error=str(exc), error_code=exc.code)
	except httpx.HTTPError as exc:
		state.update(error=f"Couldn't reach GitHub ({type(exc).__name__}). The next check will try again.",
					 error_code="network")
	save_state(state)
	return state


# ── download, verify, unpack ─────────────────────────────────────────────────
def _sha256(path: Path) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			h.update(chunk)
	return h.hexdigest()


def download(latest: dict, token: str) -> Path:
	"""The release zip into UPDATES, checked against the sha256 of latest.json, unpacked: returns the unpacked
	IndiePulse folder (what apply_update swaps in). UpdateError on any mismatch — nothing is installed then."""
	UPDATES.mkdir(parents=True, exist_ok=True)
	name, sha = latest["name"], latest["sha256"]
	if not _ZIP_RX.fullmatch(name):
		raise UpdateError(f"Not an IndiePulse zip: {name!r}")
	zpath = UPDATES / name
	if not (zpath.is_file() and _sha256(zpath) == sha):
		part = zpath.with_name(name + ".part")
		h, size = hashlib.sha256(), 0
		try:
			with _client() as client:
				r = _get(client, latest["asset_url"], token, accept="application/octet-stream", stream=True)
				try:
					if r.status_code != 200:
						raise _http_error(r, "the repository", token, name)
					with open(part, "wb") as f:
						for chunk in r.iter_bytes(1 << 16):
							size += len(chunk)
							if size > MAX_DOWNLOAD:
								raise UpdateError(f"{name} is bigger than {MAX_DOWNLOAD >> 20} MB: not an IndiePulse "
												  f"release.")
							h.update(chunk)
							f.write(chunk)
				finally:
					r.close()
			if h.hexdigest() != sha:
				raise UpdateError(f"{name} doesn't match the checksum in latest.json, so it wasn't installed "
								  f"(a damaged download?). Try again.", "checksum")
			os.replace(part, zpath)
		finally:
			part.unlink(missing_ok=True)
	for old in UPDATES.glob("IndiePulse-*.zip"):
		if old != zpath:
			old.unlink(missing_ok=True)
	return unpack(zpath, latest["version"])


def unpack(zpath: Path, version: str) -> Path:
	staging = UPDATES / "staging"
	shutil.rmtree(staging, ignore_errors=True)
	staging.mkdir(parents=True)
	try:
		with zipfile.ZipFile(zpath) as z:
			for info in z.infolist():
				parts = info.filename.replace("\\", "/").split("/")
				if info.filename.startswith(("/", "\\")) or any(p == ".." or ":" in p for p in parts):
					raise UpdateError(f"{zpath.name} has an unsafe path in it ({info.filename!r}): not installed.",
									  "bad_release")
			z.extractall(staging)
		root = find_release_root(staging)
		found = version_of(root)
		if found != version:
			raise UpdateError(f"{zpath.name} holds IndiePulse {found or '?'}, not {version}.", "bad_release")
		release_items(root)  # required files there, nothing it may not replace
	except zipfile.BadZipFile:
		raise UpdateError(f"{zpath.name} isn't a valid zip.", "bad_release")
	except UpdateError:
		raise
	except Exception as exc:  # SwapError from the checks, OSError
		raise UpdateError(f"{zpath.name} can't be installed: {exc}", "bad_release")
	write_json(UPDATES / "ready.json", {"version": version, "root": str(root)})
	return root


# ── for the web UI ───────────────────────────────────────────────────────────
def busy() -> str:
	"""What's publishing or rendering right now, '' = nothing. An install waits for it: the worker gets stopped."""
	from sqlmodel import Session, func, select
	from app.db.session import engine
	from app.models.models import Job, JobStatusEnum, Post, PostStatusEnum
	with Session(engine) as s:
		jobs = s.exec(select(func.count()).select_from(Job).where(Job.status == JobStatusEnum.running)).one()
		posts = s.exec(select(func.count()).select_from(Post).where(Post.status == PostStatusEnum.running)).one()
	if posts:
		return f"{posts} post{'s' if posts > 1 else ''} being published"
	if jobs:
		return f"{jobs} job{'s' if jobs > 1 else ''} in progress"
	return ""


def request_install() -> str:
	"""The web UI's Install: leave a request the tray picks up (only it can stop the server). '' or why not."""
	from app.desktop import is_supervised
	latest = available()
	if is_git_checkout():
		return "This is a git checkout: update it with git pull."
	if not latest:
		return "There is no newer version to install."
	if not is_supervised():
		return "Installing needs IndiePulse running from its tray icon (start IndiePulse.exe)."
	UPDATES.mkdir(parents=True, exist_ok=True)
	install_request_path().write_text(latest["version"], encoding="utf-8")
	set_phase("requested", latest["version"], f"Starting the update to IndiePulse {latest['version']}…")
	return ""


def banner(cfg: Optional[dict] = None) -> Optional[dict]:
	"""What the banner on every page says, or None. Kinds: progress (polls), done / failed (dismissable), available,
	git (available, but a checkout updates with git), need_token."""
	c = config(cfg)
	prog = progress()
	phase, pv = prog.get("phase"), str(prog.get("version") or "")
	if phase in ACTIVE:
		return {"kind": "progress", "text": prog.get("text") or f"Updating to IndiePulse {pv}…"}
	if phase in DONE and not prog.get("seen") and time.time() - (prog.get("at") or 0) < _SHOW_DONE:
		if phase == "installed" and pv == __version__:
			return {"kind": "done", "text": prog.get("text") or f"IndiePulse updated to {pv}."}
		if phase != "installed":
			return {"kind": "failed", "text": prog.get("text") or f"The update to {pv} failed."}
	if c["mode"] == "off":
		return None
	state = load_state()
	latest = available(state)
	if latest:
		return {"kind": "git" if is_git_checkout() else "available", "latest": latest}
	if state.get("error_code") == "need_token" and not is_git_checkout():
		return {"kind": "need_token", "text": state.get("error") or ""}
	return None
