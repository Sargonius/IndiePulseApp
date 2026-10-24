"""IndiePulse's own window: Edge (or Chrome) in app mode — no tabs, no address bar, its own icon in the taskbar —
instead of a tab in the user's browser.

`msedge --app=http://127.0.0.1:<port>/ --user-data-dir=<DATA>\\browser`: a profile of IndiePulse's own, so it never
mixes with the user's browsing (their extensions, sessions, history) and lives and goes with IndiePulse's data. A
second start of the same profile hands over to the running Edge and opens one more window, so "Open IndiePulse"
brings up the window that is already open (found by the processes of that profile) and starts one only when there is
none. No Edge or Chrome (or INDIEPULSE_BROWSER=default): the default browser, as before.

The tray opens it (app/desktop/tray.py); IndiePulse.exe asks the tray to (a double-click while it runs).
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import webbrowser
import winreg
from ctypes import wintypes
from typing import Optional

from loguru import logger

from app.desktop import DATA

PROFILE = DATA / "browser"
WINDOW_SIZE = "1280,860"  # the first window; Edge remembers where the user put it after that
_BROWSERS = (("msedge.exe", r"Microsoft\Edge\Application"), ("chrome.exe", r"Google\Chrome\Application"))
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


def find_browser() -> Optional[str]:
	"""Edge — on every Windows 10/11 — else Chrome; None: the default browser. INDIEPULSE_BROWSER: a Chromium
	browser's exe to use instead, or "default"."""
	forced = os.getenv("INDIEPULSE_BROWSER", "").strip().strip('"')
	if forced:
		return None if forced.lower() == "default" or not os.path.isfile(forced) else forced
	for exe, folder in _BROWSERS:
		found = _app_path(exe)
		if found:
			return found
		for base in (os.getenv("ProgramFiles(x86)"), os.getenv("ProgramFiles"), os.getenv("LOCALAPPDATA")):
			if base and os.path.isfile(os.path.join(base, folder, exe)):
				return os.path.join(base, folder, exe)
	return None


def command(url: str, browser: str, first: bool) -> list[str]:
	cmd = [browser, f"--app={url}", f"--user-data-dir={PROFILE}", "--no-first-run", "--no-default-browser-check"]
	if first:
		cmd.append(f"--window-size={WINDOW_SIZE}")
	return cmd


def _profile_pids() -> set[int]:
	"""The browser processes of IndiePulse's profile (all of them have it on their command line)."""
	import psutil
	needle = f"--user-data-dir={PROFILE}".lower()
	names = {exe for exe, _ in _BROWSERS}
	forced = os.getenv("INDIEPULSE_BROWSER", "").strip().strip('"')
	if forced:
		names.add(os.path.basename(forced).lower())
	pids = set()
	for p in psutil.process_iter(["name"]):  # the name first: a command line for every process takes long
		try:
			if (p.info["name"] or "").lower() in names and needle in " ".join(p.cmdline()).lower():
				pids.add(p.pid)
		except psutil.Error:
			continue
	return pids


def windows() -> list[int]:
	"""The app's top-level windows (topmost first): visible, unowned windows of its profile's browser processes."""
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
			_start(command(url, browser, first=not PROFILE.exists()))
			return "app"
		except OSError:
			logger.exception(f"couldn't start {browser} in app mode; the default browser then")
	webbrowser.open(url)
	return "browser"


def close_windows() -> int:
	"""Ask the app's windows to close (Quit, the uninstaller): Edge closes them as if the user had."""
	try:
		hwnds = windows()
	except Exception:
		logger.exception("looking for the IndiePulse window failed")
		return 0
	for hwnd in hwnds:
		_user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
	return len(hwnds)
