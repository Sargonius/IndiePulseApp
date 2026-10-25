"""Small Win32 helpers via ctypes (no pywin32): kill-on-close job, single instance, the events IndiePulse.exe talks
to the tray with, autostart entry, shortcuts, the uninstall entry, message box."""
from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import threading
import winreg
from ctypes import wintypes
from pathlib import Path
from typing import Callable

from app.desktop import DRIVE, HOME, PORTABLE_MARKER, ROOT
from app.desktop.apply_update import instance_key

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_k32.OpenEventW.restype = wintypes.HANDLE
_k32.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
_k32.SetEvent.argtypes = [wintypes.HANDLE]
_k32.CloseHandle.argtypes = [wintypes.HANDLE]
_k32.WaitForMultipleObjects.restype = wintypes.DWORD
_k32.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL, wintypes.DWORD]

CREATE_NO_WINDOW = 0x08000000
_ERROR_ALREADY_EXISTS = 183
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000


class _BasicLimits(ctypes.Structure):
	_fields_ = [
		("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
		("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
		("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
		("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
	]


class _ExtendedLimits(ctypes.Structure):
	_fields_ = [
		("BasicLimitInformation", _BasicLimits), ("IoInfo", ctypes.c_uint64 * 6),
		("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
		("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t),
	]


class KillOnCloseJob:
	"""Processes assigned here (and everything they spawn) die when this process dies,
	however it dies — so a crashed/killed tray never leaves a server holding the port."""

	def __init__(self) -> None:
		self._handle = _k32.CreateJobObjectW(None, None)
		if not self._handle:
			raise ctypes.WinError(ctypes.get_last_error())
		info = _ExtendedLimits()
		info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
		if not _k32.SetInformationJobObject(self._handle, _JobObjectExtendedLimitInformation,
				ctypes.byref(info), ctypes.sizeof(info)):
			raise ctypes.WinError(ctypes.get_last_error())

	def assign(self, proc: subprocess.Popen) -> bool:
		return bool(_k32.AssignProcessToJobObject(self._handle, wintypes.HANDLE(int(proc._handle))))


_mutex = None  # held for the life of the process


def acquire_single_instance(port: int) -> bool:
	"""False if another tray for this project folder + port is already running."""
	global _mutex
	key = hashlib.sha1(f"{str(ROOT).lower()}|{port}".encode()).hexdigest()[:16]
	_mutex = _k32.CreateMutexW(None, False, f"Local\\IndiePulse-{key}")
	return ctypes.get_last_error() != _ERROR_ALREADY_EXISTS


def message_box(text: str, title: str = "IndiePulse", error: bool = True) -> None:
	ctypes.windll.user32.MessageBoxW(None, text, title, 0x10 if error else 0x40)


# ── IndiePulse.exe ↔ the tray ────────────────────────────────────────────────
_EVENT_MODIFY_STATE = 0x0002
_INFINITE = 0xFFFFFFFF


def event_name(what: str) -> str:
	"""Local\\IndiePulse-<this folder's key>-<what>: the same names as launcher/Instance.cs."""
	return f"Local\\IndiePulse-{instance_key(HOME)}-{what}"


class InstanceEvents:
	"""What IndiePulse.exe says to the running tray of its folder. The tray creates two named events: `show` (the exe
	was started again: bring up the window; the exe takes the event's existence for "IndiePulse runs") and `quit`
	(the uninstaller). `ready` is the exe's: it waits on it with its setup window open, and the tray sets it once the
	server answers. Handles are closed by the process ending, which is how the exe sees the tray gone."""

	def __init__(self) -> None:
		self._show = _k32.CreateEventW(None, False, False, event_name("show"))   # auto-reset: one per start
		self._quit = _k32.CreateEventW(None, True, False, event_name("quit"))    # manual-reset

	def listen(self, on_show: Callable[[], None], on_quit: Callable[[], None]) -> None:
		if not (self._show and self._quit):
			return
		handles = (wintypes.HANDLE * 2)(self._show, self._quit)

		def wait() -> None:
			while True:
				which = _k32.WaitForMultipleObjects(2, handles, False, _INFINITE)
				if which == 0:
					on_show()
				elif which == 1:
					on_quit()
					return
				else:  # WAIT_FAILED: nothing to listen to
					return
		threading.Thread(target=wait, name="exe-events", daemon=True).start()

	@staticmethod
	def signal_ready() -> bool:
		"""The server is up: the exe that started this tray closes its window (no exe waiting: nothing to do)."""
		h = _k32.OpenEventW(_EVENT_MODIFY_STATE, False, event_name("ready"))
		if not h:
			return False
		try:
			return bool(_k32.SetEvent(h))
		finally:
			_k32.CloseHandle(h)


# ── How IndiePulse is started ────────────────────────────────────────────────
def exe_path() -> Path:
	return HOME / "IndiePulse.exe"


def launch_command(background: bool = False) -> tuple[str, str]:
	"""(executable, arguments) that start IndiePulse without a console window: IndiePulse.exe — or, in a checkout where
	it isn't built, the venv's pythonw with app/desktop/launch.py."""
	if exe_path().is_file():
		return str(exe_path()), "--background" if background else ""
	from app.desktop import deps
	args = f'"{ROOT / "app" / "desktop" / "launch.py"}"' + (" --background" if background else "")
	return str(deps.venv_python(windowed=True)), args


def _command_line(background: bool = False) -> str:
	exe, args = launch_command(background)
	return f'"{exe}" {args}'.rstrip()


def _ours(command: str) -> bool:
	"""A command that starts this copy (and not another IndiePulse folder's)."""
	c = command.lower()
	return str(HOME).lower() in c or str(ROOT).lower() in c


# ── Autostart (HKCU Run) ─────────────────────────────────────────────────────
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "IndiePulse"


def autostart_command() -> str:
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
			return str(winreg.QueryValueEx(k, _RUN_VALUE)[0])
	except OSError:
		return ""


def autostart_enabled() -> bool:
	return bool(autostart_command())


def set_autostart(enabled: bool) -> None:
	with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
		if enabled:
			winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, _command_line(background=True))
		else:
			try:
				winreg.DeleteValue(k, _RUN_VALUE)
			except FileNotFoundError:
				pass


def refresh_autostart() -> bool:
	"""An autostart entry of this copy that starts it the old way (IndiePulse.pyw before IndiePulse.exe; pythonw before
	the exe was built) is rewritten to the way it starts now. True when it was."""
	current = autostart_command()
	if current and _ours(current) and current != _command_line(background=True):
		set_autostart(True)
		return True
	return False


# ── Shortcuts ────────────────────────────────────────────────────────────────
def _machine_id() -> str:
	try:
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
							winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
			return str(winreg.QueryValueEx(k, "MachineGuid")[0])
	except OSError:
		return ""


def trusted_pc() -> bool:
	"""IndiePulse may leave things on this computer — shortcuts, the autostart entry. With trusted_pcs in the drive
	settings (IndiePulse.portable.ini): only when that list has this one; its lines are «NAME|MachineGuid», checked by
	the GUID (a club computer may well have the home one's name). Without: an installed copy or a checkout may, a
	portable one (portable.txt: its setup promised to write nothing to the computer) may not."""
	if not DRIVE.trusted_pcs:
		return not PORTABLE_MARKER.exists()
	try:
		lines = DRIVE.trusted_pcs.read_text(encoding="utf-8-sig").splitlines()
	except OSError:
		return False
	me = _machine_id().lower()
	return bool(me) and any(line.partition("|")[2].strip().lower() == me for line in lines)


def _known_folder(ps_name: str) -> Path:
	out = subprocess.check_output(
		["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{ps_name}')"],
		creationflags=CREATE_NO_WINDOW, text=True)
	return Path(out.strip())


def shortcut_paths() -> list[Path]:
	return [_known_folder("Desktop") / "IndiePulse.lnk", _known_folder("Programs") / "IndiePulse.lnk"]


def create_shortcuts(icon_path: Path, paths: list[Path] | None = None) -> list[Path]:
	"""IndiePulse shortcuts (the desktop and the Start menu, or `paths`) that start it as launch_command() says; the
	exe's own icon when there is the exe."""
	exe, args = launch_command()
	icon = exe_path() if exe_path().is_file() else icon_path
	paths = shortcut_paths() if paths is None else paths
	q = lambda s: str(s).replace("'", "''")  # noqa: E731  (PowerShell single-quote escape)
	script = "$sh = New-Object -ComObject WScript.Shell\n" + "".join(
		f"$s = $sh.CreateShortcut('{q(p)}'); $s.TargetPath = '{q(exe)}'; $s.Arguments = '{q(args)}'; "
		f"$s.WorkingDirectory = '{q(HOME)}'; $s.IconLocation = '{q(icon)},0'; "
		f"$s.Description = 'IndiePulse'; $s.Save()\n"
		for p in paths)
	subprocess.check_call(["powershell", "-NoProfile", "-Command", script], creationflags=CREATE_NO_WINDOW)
	return paths


def shortcut_targets(paths: list[Path]) -> dict[Path, str]:
	"""The program each shortcut of `paths` that is there starts ("" when it starts none)."""
	there = [p for p in paths if p.exists()]
	if not there:
		return {}
	q = lambda s: str(s).replace("'", "''")  # noqa: E731  (PowerShell single-quote escape)
	script = ("[Console]::OutputEncoding = [Text.Encoding]::UTF8\n$sh = New-Object -ComObject WScript.Shell\n" +
			  "".join(f"'>' + $sh.CreateShortcut('{q(p)}').TargetPath\n" for p in there))
	out = subprocess.check_output(["powershell", "-NoProfile", "-Command", script], creationflags=CREATE_NO_WINDOW,
								  text=True, encoding="utf-8")
	targets = [line[1:].strip() for line in out.splitlines() if line.startswith(">")]
	return dict(zip(there, targets))


def _inside(path: str, folder: Path) -> bool:
	p, f = (os.path.normcase(os.path.abspath(str(x))).rstrip("\\/") for x in (path, folder))
	return p == f or p.startswith(f + "\\")


def _starts_another_copy(target: str) -> bool:
	"""A shortcut's program that isn't this copy's (its IndiePulse.exe, or its venv's pythonw of before) and is there:
	another IndiePulse folder on the drive (a second copy, a test one) — not one gone, or under another day's drive
	letter."""
	return bool(target) and not (_inside(target, HOME) or _inside(target, ROOT)) and Path(target).exists()


def free_shortcut_paths(paths: list[Path]) -> tuple[list[Path], dict[Path, str]]:
	"""`paths` split into those this copy may make or overwrite and those that start another copy ({path: its
	program}), which a run of its own doesn't take over."""
	taken = {p: t for p, t in shortcut_targets(paths).items() if _starts_another_copy(t)}
	return [p for p in paths if p not in taken], taken


def shortcuts_stamp() -> str:
	"""What the tray's shortcut marker keeps: the command its shortcuts were made for."""
	return _command_line()


def refresh_shortcuts(marker: Path, icon_path: Path) -> list[Path]:
	"""Shortcuts the tray made for an older way of starting (the marker says which) are made again, those that are
	still there and don't start another copy now: after IndiePulse.pyw gave way to IndiePulse.exe, or the exe got
	built in a checkout."""
	try:
		made_for = marker.read_text(encoding="utf-8").strip()
	except OSError:
		return []
	if made_for == shortcuts_stamp():
		return []
	there = free_shortcut_paths([p for p in shortcut_paths() if p.exists()])[0]
	if there:
		create_shortcuts(icon_path, there)
	marker.write_text(shortcuts_stamp(), encoding="utf-8")
	return there


# ── The uninstall entry (Settings → Apps), written by IndiePulse.exe's Install ─
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\IndiePulse"


def _same_path(a: str, b: Path) -> bool:
	try:
		return bool(a) and os.path.normcase(os.path.abspath(a.rstrip("\\/"))) == os.path.normcase(str(b))
	except (OSError, ValueError):
		return False


def installed_here() -> bool:
	"""This copy is the one IndiePulse.exe installed for this user (its uninstall entry points here)."""
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _UNINSTALL_KEY) as k:
			return _same_path(str(winreg.QueryValueEx(k, "InstallLocation")[0]), HOME)
	except OSError:
		return False


def refresh_uninstall_entry(version: str) -> bool:
	"""Settings → Apps shows the version that runs: an update replaces the code, not the entry. True when changed."""
	if not installed_here():
		return False
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _UNINSTALL_KEY, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as k:
			try:
				if str(winreg.QueryValueEx(k, "DisplayVersion")[0]) == version:
					return False
			except OSError:
				pass
			winreg.SetValueEx(k, "DisplayVersion", 0, winreg.REG_SZ, version)
			return True
	except OSError:
		return False
