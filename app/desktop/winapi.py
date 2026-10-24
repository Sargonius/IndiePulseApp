"""Small Win32 helpers via ctypes (no pywin32): kill-on-close job, single instance,
autostart entry, shortcuts, message box."""
from __future__ import annotations

import ctypes
import hashlib
import subprocess
import winreg
from ctypes import wintypes
from pathlib import Path

from app.desktop import KIT, ROOT

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateJobObjectW.restype = wintypes.HANDLE
_k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
_k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
_k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
_k32.CreateMutexW.restype = wintypes.HANDLE
_k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]

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


# ── Autostart (HKCU Run) ─────────────────────────────────────────────────────
_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VALUE = "IndiePulse"


def launch_command(background: bool = False) -> tuple[str, str]:
	"""(executable, arguments) that start IndiePulse without a console window."""
	exe = str(ROOT / ".venv" / "Scripts" / "pythonw.exe")
	args = f'"{ROOT / "IndiePulse.pyw"}"' + (" --background" if background else "")
	return exe, args


def autostart_enabled() -> bool:
	try:
		with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
			winreg.QueryValueEx(k, _RUN_VALUE)
			return True
	except OSError:
		return False


def set_autostart(enabled: bool) -> None:
	with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
		if enabled:
			exe, args = launch_command(background=True)
			winreg.SetValueEx(k, _RUN_VALUE, 0, winreg.REG_SZ, f'"{exe}" {args}')
		else:
			try:
				winreg.DeleteValue(k, _RUN_VALUE)
			except FileNotFoundError:
				pass


# ── Shortcuts ────────────────────────────────────────────────────────────────
def _machine_id() -> str:
	try:
		with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
							winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
			return str(winreg.QueryValueEx(k, "MachineGuid")[0])
	except OSError:
		return ""


def trusted_pc() -> bool:
	"""Not someone else's computer the drive only visits: ClubKit has no list of trusted computers next to IndiePulse,
	or this one is on it. Its lines are «NAME|MachineGuid», checked by the GUID as ClubKit does (a club computer may
	well have the home one's name)."""
	listed = KIT / "data" / "trusted-pcs.txt"
	try:
		lines = listed.read_text(encoding="utf-8-sig").splitlines()
	except OSError:
		return not KIT.is_dir()
	me = _machine_id().lower()
	return bool(me) and any(line.partition("|")[2].strip().lower() == me for line in lines)


def _known_folder(ps_name: str) -> Path:
	out = subprocess.check_output(
		["powershell", "-NoProfile", "-Command", f"[Environment]::GetFolderPath('{ps_name}')"],
		creationflags=CREATE_NO_WINDOW, text=True)
	return Path(out.strip())


def shortcut_paths() -> list[Path]:
	return [_known_folder("Desktop") / "IndiePulse.lnk", _known_folder("Programs") / "IndiePulse.lnk"]


def create_shortcuts(icon_path: Path) -> list[Path]:
	exe, args = launch_command()
	paths = shortcut_paths()
	q = lambda s: str(s).replace("'", "''")  # noqa: E731  (PowerShell single-quote escape)
	script = "$sh = New-Object -ComObject WScript.Shell\n" + "".join(
		f"$s = $sh.CreateShortcut('{q(p)}'); $s.TargetPath = '{q(exe)}'; $s.Arguments = '{q(args)}'; "
		f"$s.WorkingDirectory = '{q(ROOT)}'; $s.IconLocation = '{q(icon_path)},0'; "
		f"$s.Description = 'IndiePulse'; $s.Save()\n"
		for p in paths)
	subprocess.check_call(["powershell", "-NoProfile", "-Command", script], creationflags=CREATE_NO_WINDOW)
	return paths
