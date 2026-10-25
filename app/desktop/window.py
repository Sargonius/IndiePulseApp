"""IndiePulse's own window: a Chromium browser in app mode — no tabs, no address bar, its own icon in the taskbar —
instead of a tab in the user's browser. The user's default browser when it is a Chromium one with app mode (Chrome,
Edge, Brave, Vivaldi, Chromium, Yandex), else Edge (on every Windows 10/11), else Chrome: find_browser.

`chrome --app=http://127.0.0.1:<port>/… --user-data-dir=<DATA>\\browser-chrome`: a profile of IndiePulse's own, so it
never mixes with the user's browsing (their extensions, sessions, history) and lives and goes with IndiePulse's data.
One per browser: Edge's is `browser\\`, the one the window always had (with its sign-ins); any other browser's is
`browser-<its exe>`, so two browsers never open one profile. A second start of the same profile hands over to the
running browser and opens one more window, so "Open IndiePulse" brings up the window that is already open (found by
the processes of any of these profiles: one left from before the default browser changed too) and starts one only
when there is none. No Chromium browser (or INDIEPULSE_BROWSER=default): the default browser, as before.

The window opens through start_url, which marks it (a cookie in its profile): its links to other sites then open in
the default browser, where the user is signed in (base.html → /api/system/open_external → open_external).

The tray opens it (app/desktop/tray.py); IndiePulse.exe asks the tray to (a double-click while it runs).
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import sys
import webbrowser
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode, urlsplit, urlunsplit

from loguru import logger

from app.desktop import DATA

PROFILE = DATA / "browser"  # Edge's; another browser's is browser-<its exe> next to it (profile())
WINDOW_SIZE = "1280,860"  # the first window; the browser remembers where the user put it after that
# Chromium browsers known to take --app and --user-data-dir: the default browser opens the window when it is one of
# these (by the exe's name; Yandex's is browser.exe, so only in a Yandex folder). Firefox and the rest don't.
_APP_MODE = ("chrome.exe", "msedge.exe", "brave.exe", "vivaldi.exe", "chromium.exe", "browser.exe")
_BROWSERS = (("msedge.exe", r"Microsoft\Edge\Application"), ("chrome.exe", r"Google\Chrome\Application"))
# the default browser: the ProgId of the user's choice for https links, then that ProgId's open command
_USER_CHOICE = r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice"
# where the window opens: marks it as IndiePulse's (app/routers/api/system.py), then goes on to the page
START = "/api/system/app_window"
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
_BREAKAWAY = 0x01000000              # CREATE_BREAKAWAY_FROM_JOB: the window outlives a job the tray may be in

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_user32.EnumWindows.argtypes = [_WNDENUMPROC, wintypes.LPARAM]
_user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
_user32.GetWindow.restype = wintypes.HWND
_user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
_user32.IsWindowVisible.argtypes = [wintypes.HWND]
_user32.IsIconic.argtypes = [wintypes.HWND]
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.SetForegroundWindow.argtypes = [wintypes.HWND]
_user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_GW_OWNER, _SW_RESTORE, _WM_CLOSE = 4, 9, 0x0010
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetCurrentProcess.restype = wintypes.HANDLE
_k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]


def _app_path(exe: str) -> Optional[str]:
	"""Where Windows' App Paths say `exe` is (what Win+R uses), per user first."""
	sub = rf"Software\Microsoft\Windows\CurrentVersion\App Paths\{exe}"
	for hive, flags in ((winreg.HKEY_CURRENT_USER, 0), (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
						(winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY)):
		try:
			with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ | flags) as k:
				path = str(winreg.QueryValueEx(k, "")[0]).strip().strip('"')
		except OSError:
			continue
		if path and os.path.isfile(path):
			return path
	return None


def _reg_value(hive: int, sub: str, name: str = "") -> str:
	"""A registry value as text ("": not there), %VARIABLES% of an expandable one filled in."""
	try:
		with winreg.OpenKey(hive, sub, 0, winreg.KEY_READ) as k:
			value, kind = winreg.QueryValueEx(k, name)
	except OSError:
		return ""
	value = str(value).strip()
	return os.path.expandvars(value) if kind == winreg.REG_EXPAND_SZ else value


def _program(command: str) -> str:
	"""The program of an open command: `"C:\\…\\chrome.exe" --single-argument %1` → C:\\…\\chrome.exe."""
	if command.startswith('"'):
		return command[1:].split('"', 1)[0]
	m = re.match(r"(.+?\.exe)(?=\s|$)", command, re.IGNORECASE)
	return m.group(1) if m else command.split(" ", 1)[0]


def default_browser() -> Optional[str]:
	"""The exe of the user's default browser, the one https links open in: the ProgId the user chose, then that
	ProgId's open command — per user, then the machine's. None: no choice made, or its program isn't there."""
	prog_id = _reg_value(winreg.HKEY_CURRENT_USER, _USER_CHOICE, "ProgId")
	if not prog_id:
		return None
	sub = rf"{prog_id}\shell\open\command"
	for hive, key in ((winreg.HKEY_CURRENT_USER, rf"Software\Classes\{sub}"),
					  (winreg.HKEY_LOCAL_MACHINE, rf"Software\Classes\{sub}"), (winreg.HKEY_CLASSES_ROOT, sub)):
		command = _reg_value(hive, key)
		exe = _program(command) if command else ""
		if exe and os.path.isfile(exe):
			return exe
	return None


def has_app_mode(exe: str) -> bool:
	"""`exe` is one of the Chromium browsers known to open IndiePulse's window (_APP_MODE)."""
	name = os.path.basename(exe).lower()
	if name == "browser.exe":
		return "yandex" in exe.lower()
	return name in _APP_MODE


def find_browser() -> Optional[str]:
	"""The browser of IndiePulse's window: INDIEPULSE_BROWSER (a Chromium browser's exe to use instead, or
	"default"); else the default browser when it has app mode; else Edge — on every Windows 10/11 — else Chrome.
	None: the default browser, as a tab."""
	forced = os.getenv("INDIEPULSE_BROWSER", "").strip().strip('"')
	if forced:
		return None if forced.lower() == "default" or not os.path.isfile(forced) else forced
	try:
		default = default_browser()
	except Exception:
		logger.exception("reading the default browser failed")
		default = None
	if default and has_app_mode(default):
		return default
	for exe, folder in _BROWSERS:
		found = _app_path(exe)
		if found:
			return found
		for base in (os.getenv("ProgramFiles(x86)"), os.getenv("ProgramFiles"), os.getenv("LOCALAPPDATA")):
			if base and os.path.isfile(os.path.join(base, folder, exe)):
				return os.path.join(base, folder, exe)
	return None


def profile(browser: str) -> Path:
	"""The window's profile for `browser`: Edge keeps browser\\, the one it always had; any other browser gets
	browser-<its exe> (browser-chrome), so a Chrome never opens Edge's."""
	stem = Path(browser).stem.lower()
	return PROFILE if stem == "msedge" else PROFILE.with_name(f"{PROFILE.name}-{stem}")


def is_profile(path: str) -> bool:
	"""`path` is one of the window's profiles: browser\\ or browser-<exe>\\ of the data folder, whichever browser."""
	ours = os.path.normcase(os.path.normpath(str(PROFILE)))
	p = os.path.normcase(os.path.normpath(path.strip('"') or "."))
	folder, name = os.path.split(p)
	prefix = os.path.basename(ours) + "-"
	return p == ours or (folder == os.path.dirname(ours) and name.startswith(prefix) and len(name) > len(prefix))


def start_url(url: str) -> str:
	"""Where the window opens `url`: through START, which marks it as IndiePulse's window (a cookie in its own profile;
	links to other sites then go to the default browser) and goes on to `url`'s page."""
	parts = urlsplit(url)
	page = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
	return urlunsplit((parts.scheme, parts.netloc, START, urlencode({"next": page}), ""))


def command(url: str, browser: str, first: bool) -> list[str]:
	cmd = [browser, f"--app={url}", f"--user-data-dir={profile(browser)}", "--no-first-run",
		   "--no-default-browser-check"]
	if first:
		cmd.append(f"--window-size={WINDOW_SIZE}")
	return cmd


def _has_profile(cmdline: list[str]) -> bool:
	for arg in cmdline:
		if arg.lower().startswith("--user-data-dir="):
			return is_profile(arg.split("=", 1)[1])
	return False


def _profile_pids() -> set[int]:
	"""The browser processes of IndiePulse's profiles (all of them have it on their command line), whichever browser:
	a window still open from before the default browser changed is found too."""
	import psutil
	names = set(_APP_MODE)
	forced = os.getenv("INDIEPULSE_BROWSER", "").strip().strip('"')
	if forced:
		names.add(os.path.basename(forced).lower())
	pids = set()
	for p in psutil.process_iter(["name"]):  # the name first: a command line for every process takes long
		try:
			if (p.info["name"] or "").lower() in names and _has_profile(p.cmdline()):
				pids.add(p.pid)
		except psutil.Error:
			continue
	return pids


def windows() -> list[int]:
	"""The app's top-level windows (topmost first): visible, unowned windows of its profiles' browser processes."""
	pids = _profile_pids()
	if not pids:
		return []
	found: list[int] = []
	buf = ctypes.create_unicode_buffer(64)

	def visit(hwnd, _lparam):
		pid = wintypes.DWORD()
		_user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
		if pid.value in pids and _user32.IsWindowVisible(hwnd) and not _user32.GetWindow(hwnd, _GW_OWNER):
			_user32.GetClassNameW(hwnd, buf, len(buf))
			if buf.value.startswith("Chrome_WidgetWin"):
				found.append(hwnd)
		return True
	_user32.EnumWindows(_WNDENUMPROC(visit), 0)
	return found


def bring_to_front() -> bool:
	"""The window that is open, restored and in front; False: there is none. Windows lets a process take the
	foreground only right after the user did something with it: a click in the tray menu, or IndiePulse.exe, which
	allows it (AllowSetForegroundWindow) before it asks the tray."""
	try:
		hwnds = windows()
	except Exception:
		logger.exception("looking for the IndiePulse window failed")
		return False
	if not hwnds:
		return False
	hwnd = hwnds[0]
	if _user32.IsIconic(hwnd):
		_user32.ShowWindow(hwnd, _SW_RESTORE)
	_user32.SetForegroundWindow(hwnd)
	return True


def _start(cmd: list[str]) -> None:
	kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
	try:
		subprocess.Popen(cmd, creationflags=_DETACHED | _BREAKAWAY, **kw)
	except OSError:  # the job we're in doesn't allow breaking away
		subprocess.Popen(cmd, creationflags=_DETACHED, **kw)


def open_window(url: str, new: bool = False) -> str:
	"""Bring up IndiePulse's window, or open it at `url`; `new`: another window at `url` either way (a link from the
	tray menu). Returns how: "front" | "app" | "browser"."""
	if not new and bring_to_front():
		return "front"
	browser = find_browser()
	if browser:
		try:
			_start(command(start_url(url), browser, first=not profile(browser).exists()))
			return "app"
		except OSError:
			logger.exception(f"couldn't start {browser} in app mode; the default browser then")
	webbrowser.open(url)
	return "browser"


def close_windows() -> int:
	"""Ask the app's windows to close (Quit, the uninstaller): the browser closes them as if the user had."""
	try:
		hwnds = windows()
	except Exception:
		logger.exception("looking for the IndiePulse window failed")
		return 0
	for hwnd in hwnds:
		_user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
	return len(hwnds)


# ── links from the window to other sites ─────────────────────────────────────
_OPEN = "import os, sys; os.startfile(sys.argv[1])"


def _in_job() -> bool:
	inside = wintypes.BOOL()
	return bool(_k32.IsProcessInJob(_k32.GetCurrentProcess(), None, ctypes.byref(inside))) and bool(inside)


def open_external(url: str) -> None:
	"""`url` (checked by the caller: http, https or mailto) in the user's default browser or mail app, as Windows
	opens a link. Windows starts the browser, when it isn't running yet, from the process that asks — and the server
	is in the tray's kill-on-close job (winapi.KillOnCloseJob), so a browser started from there would close with
	IndiePulse. A pythonw that leaves the job asks instead; the server itself when it is in no job (started by hand),
	or the job doesn't let it go. OSError: Windows couldn't open it."""
	if _in_job():
		python = Path(sys.executable).with_name("pythonw.exe")
		cmd = [str(python if python.is_file() else sys.executable), "-I", "-c", _OPEN, url]
		try:
			proc = subprocess.Popen(cmd, creationflags=_DETACHED | _BREAKAWAY, stdin=subprocess.DEVNULL,
									stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
		except OSError:
			logger.warning("can't leave the job to open a link: opening it from the server")
		else:
			try:
				code = proc.wait(10)
			except subprocess.TimeoutExpired:
				return  # Windows is still at it: the browser comes up when it does
			if code:
				raise OSError(f"no program took it (exit code {code})")
			return
	os.startfile(url)
