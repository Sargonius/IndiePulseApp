"""The tray's half of updating: when to check, when to install (app/services/updates.py talks to GitHub,
app/desktop/apply_update.py swaps the code).

The checks run here, in a thread of the tray — not in the worker or the server: the tray is the one process that
lives as long as IndiePulse does (Restart, a crash or an update start the server and the worker over, and their timers
with them; Huey's crontab can't say "30 s after start" either), the worker's single queue is for publishing, not for
a download — and the install has to start from here anyway: stop the server and the worker, hand over, quit.

- 30 s after start, then every 6 h, unless update_mode is off or this is a git checkout (that updates with git);
- notify (the default): a balloon once per version; the banner in the app and "Install update vX" in this menu;
- auto: install right away, as soon as nothing is publishing — unless that version already failed to start here;
- install (the banner's or Settings' button, which leave install.request; or the menu): download and verify while
  the app runs, wait until nothing is publishing (a manual install waits MANUAL_WAIT at most), start apply_update.py,
  stop the server and the worker (the worker finishes its task), quit. apply_update waits for this process to end
  before it touches a file, and starts the new tray itself.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Callable

from loguru import logger

from app import __version__
from app.desktop import DATA, HOME, apply_update, deps
from app.services import updates

MANUAL_WAIT = 15 * 60  # s an install the user asked for waits for a publish in progress, then goes ahead anyway
_TICK = 5              # s between looks at install.request
_IDLE_POLL = 15        # s between "is anything publishing?" while an install waits


class TrayUpdater:
	def __init__(self, sup, *, notify: Callable[[str], None], refresh: Callable[[], None], base_url: str):
		self.sup, self.notify, self.refresh = sup, notify, refresh
		self.base_url = base_url.rstrip("/") + "/"
		self.installing = False
		self._lock = threading.Lock()
		self._notified = set(updates.load_state().get("notified") or [])
		self.sleep = time.sleep

	# ── for the menu (any thread) ────────────────────────────────────────────
	def install_label(self) -> str:
		latest = updates.available()
		return f"Install update v{latest['version']}" if latest else "Install update"

	def can_install(self) -> bool:
		return not self.installing and not updates.is_git_checkout() and updates.available() is not None

	def check_now(self) -> None:
		self._spawn(self.check, True)

	def install(self) -> None:
		self._start_install(manual=True)

	def start(self) -> None:
		self._spawn(self._run)

	# ── the thread ───────────────────────────────────────────────────────────
	def _spawn(self, fn, *args) -> None:
		def guarded() -> None:
			try:
				fn(*args)
			except Exception:
				logger.exception("updater failed")
		threading.Thread(target=guarded, name="updater", daemon=True).start()

	def _run(self) -> None:
		next_check = time.monotonic() + updates.FIRST_CHECK_AFTER
		while True:
			due = time.monotonic() >= next_check
			if due:
				next_check = time.monotonic() + updates.CHECK_EVERY
			try:
				self.tick(due)
			except Exception:
				logger.exception("updater tick failed")
			self.sleep(_TICK)

	def tick(self, check_due: bool) -> None:
		self._report_result()
		if self._take_request():
			self._start_install(manual=True)
		if check_due and updates.config()["mode"] != "off" and not updates.is_git_checkout():
			self.check()

	def check(self, manual: bool = False) -> dict:
		state = updates.check()
		latest = updates.available(state)
		mode = updates.config()["mode"]
		git = updates.is_git_checkout()
		version = latest["version"] if latest else ""
		if manual:
			if state.get("error"):
				self.notify(f"Update check: {state['error']}")
			elif latest and git:
				self.notify(f"IndiePulse {version} is available. This is a git checkout: update it with git pull.")
			elif latest:
				self.notify(f"IndiePulse {version} is available: “Install update” in this menu, or in the app.")
			else:
				self.notify(f"IndiePulse {__version__} is the latest version.")
		elif latest and mode == "notify" and not git and version not in self._notified:
			self._notified.add(version)
			state["notified"] = sorted(self._notified)
			updates.save_state(state)
			self.notify(f"IndiePulse {version} is available: “Install update” in the tray menu or the app's banner.")
		if latest and mode == "auto" and not git and not self._failed_here(version):
			logger.info(f"update {version} found, auto mode: installing when nothing is publishing")
			self._start_install(manual=False)
		self.refresh()
		return state

	def _failed_here(self, version: str) -> bool:
		"""This version was installed and didn't start (or couldn't be swapped in): auto mode doesn't retry it —
		every try would stop the app for nothing. A failed download is retried at the next check."""
		p = updates.progress()
		return p.get("version") == version and p.get("phase") in ("rolled_back", "failed") \
			and p.get("stage") != "download"

	def _take_request(self) -> bool:
		path = updates.install_request_path()
		if not path.exists():
			return False
		try:
			path.unlink()
		except OSError:
			pass
		return True

	def _report_result(self) -> None:
		"""One balloon for how the last install went (apply_update writes it; this may be the tray it started)."""
		p = updates.progress()
		if p.get("phase") in updates.DONE and not p.get("reported") and p.get("stage") != "download":
			updates.mark_progress(reported=True)
			if p.get("text"):
				self.notify(p["text"])

	def _start_install(self, manual: bool) -> None:
		with self._lock:
			if self.installing:
				return
			self.installing = True
		self._spawn(self._install, manual)

	def _install(self, manual: bool) -> None:
		launched, version = False, ""
		try:
			latest = updates.available()
			if updates.is_git_checkout() or latest is None:
				if manual:
					self.notify("This is a git checkout: update it with git pull." if latest else
								"There is no newer version to install.")
				return
			version = latest["version"]
			updates.set_phase("downloading", version, f"Downloading IndiePulse {version}…")
			self.refresh()
			try:
				staged = updates.download(latest, updates.config()["token"])
			except updates.UpdateError as exc:
				updates.set_phase("failed", version, f"Couldn't download IndiePulse {version}: {exc}", stage="download")
				self.notify(f"Couldn't download IndiePulse {version}: {exc}")
				return
			self._wait_until_idle(version, manual)
			updates.set_phase("installing", version, f"Installing IndiePulse {version}: it restarts in a moment…",
							  from_version=__version__)
			# the installer first: if it can't start, nothing has been stopped yet
			apply_update.launch(python=deps.venv_python(windowed=True), work=updates.UPDATES, root=HOME, data=DATA,
								staged=staged, version=version, from_version=__version__, pid=os.getpid(),
								url=self.base_url)
			launched = True
			logger.info(f"installing {version}: installer started, stopping server and worker")
			self.notify(f"Installing IndiePulse {version}: it restarts in a moment.")
			self.sup.quit()  # the worker finishes its task; then the tray exits and the installer goes on
		except Exception as exc:
			logger.exception("update failed")
			if version:
				updates.set_phase("failed", version, f"The update to {version} failed: {exc}", stage="download")
			self.notify(f"The update failed: {exc} (logs\\launcher.log)")
		finally:
			if not launched:
				self.installing = False
				self.refresh()

	def _wait_until_idle(self, version: str, manual: bool) -> None:
		deadline = time.monotonic() + MANUAL_WAIT if manual else None
		while True:
			what = self._busy(manual)
			if not what:
				return
			if deadline is not None and time.monotonic() > deadline:
				logger.warning(f"installing {version} anyway after {MANUAL_WAIT} s: {what}")
				return
			updates.set_phase("waiting", version, f"Waiting for {what} to finish, then installing IndiePulse "
							  f"{version}…")
			self.sleep(_IDLE_POLL)

	def _busy(self, manual: bool) -> str:
		"""What's publishing (the server knows: the DB), '' = nothing."""
		if self.sup.phase != "running":
			return "IndiePulse to finish starting"
		try:
			with urllib.request.urlopen(self.base_url + "api/updates/busy", timeout=5) as r:
				return str(json.loads(r.read().decode("utf-8")).get("busy") or "")
		except Exception:
			# no answer: the user's Install goes ahead (stopping the worker still lets its task finish);
			# auto mode waits until the server can say
			return "" if manual else "the server to answer"
