"""Keep the venv in sync with requirements.lock. Stdlib only: app/desktop/launch.py imports it before the venv (and
pystray/psutil) exist, and the updater runs it with -I -S.

IndiePulse.exe makes the venv before anything of this runs (launcher/Runtime.cs does what ensure_venv does here, with
the same uv, folders and stamp). What is left here: the tray syncing a changed requirements.lock, the updater
installing a new version's, and a checkout started without the exe (launch.py).
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from app.core.paths import ROOT, RUNTIME, is_release_layout
from app.desktop import KIT

# A release keeps its venv in runtime\ (lib\, the code, is replaced whole by an update); a checkout has its .venv
VENV = RUNTIME / "venv" if is_release_layout() else ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"  # what the app needs: edit this one
# The exact versions the app is tested with, all of them (dependencies of dependencies too): what gets installed.
# `~=` in requirements.txt alone let a fresh install take sqlmodel 0.0.48, which rejects naive datetimes.
LOCK = ROOT / "requirements.lock"
_STAMP = VENV / "indiepulse-requirements.sha256"
PYTHON_VERSION = "3.12"  # the one requirements.lock is tested with
# --no-bin: no python3.12.exe left in the profile's .local\bin; --no-registry: no PEP 514 entry under
# HKCU\Software\Python (a portable copy writes nothing to the computer). IndiePulse.exe installs it the same way.
_INSTALL_PYTHON = ["python", "install", "--no-bin", "--no-registry"]

CREATE_NO_WINDOW = 0x08000000


def venv_python(windowed: bool = False) -> Path:
	return VENV / "Scripts" / ("pythonw.exe" if windowed else "python.exe")


def _venv_home() -> str:
	"""The base Python's folder that pyvenv.cfg names ('' without a venv)."""
	try:
		cfg = (VENV / "pyvenv.cfg").read_text(encoding="utf-8")
	except OSError:
		return ""
	return next((v.strip() for k, _, v in (line.partition("=") for line in cfg.splitlines()) if k.strip() == "home"), "")


def venv_alive() -> bool:
	"""The venv runs on this computer. One made on another (its Python in another user's profile, the drive under
	another letter) still has its python.exe, which only says «No Python at …»: the base Python pyvenv.cfg names
	must be here."""
	home = _venv_home()
	return bool(home) and (Path(home) / "python.exe").is_file() and venv_python().is_file()


def _repair_python(log_path: Path) -> bool:
	"""uv's Python lost its python.exe — Kaspersky took the drive's one for a trojan once (2026-10-24) and deleted it
	at reboot: uv puts it back from its cache."""
	exe = uv()
	ok = bool(exe) and _call([exe, *_INSTALL_PYTHON, "--reinstall", PYTHON_VERSION], log_path, uv_env())
	_drop_temp_cache()
	return ok


def uv() -> str | None:
	"""The uv IndiePulse.exe uses: ClubKit's own copy on the drive, else the one it put in runtime\\ — else uv from PATH
	(a checkout). No Python needs to be installed on the computer with any of them."""
	for candidate in (KIT / "tools" / "uv" / "uv.exe", RUNTIME / "uv" / "uv.exe"):
		if candidate.is_file():
			return str(candidate)
	return shutil.which("uv")


DEEP_CACHE = 100  # characters: see uv_cache()


def uv_cache() -> Path:
	"""The runtime's uv cache: runtime\\cache — unless that is deep in the folders. A package without a wheel (huey) is
	built ~130 characters further down in it, which must stay under Windows' 260; then a short one in %TEMP%, removed
	once the packages are in. The same rule as IndiePulse.exe's (launcher/Runtime.cs, Cache)."""
	own = RUNTIME / "cache"
	if len(str(own)) <= DEEP_CACHE:
		return own
	from app.desktop.apply_update import instance_key
	return Path(tempfile.gettempdir()).resolve() / f"IndiePulse-uv-{instance_key(RUNTIME.parent)[:8]}"


def _drop_temp_cache() -> None:
	"""uv_cache()'s short one in %TEMP%, once a step that may have used it is done: the venv has what it needs (a copy
	or a hard link of each file), and nothing of a portable copy stays on the computer."""
	if KIT.is_dir() or not RUNTIME.is_dir():
		return
	cache = uv_cache()
	if RUNTIME not in cache.parents:
		shutil.rmtree(cache, ignore_errors=True)


def uv_env() -> dict[str, str]:
	"""Where uv keeps its cache and Pythons. ClubKit: on the drive (not in the profile of a computer it will leave), and
	only those — a venv made on one computer then runs on the next, if the drive has the same letter there; the same
	three as ClubKit's Get-UvEnvironment. Else, when IndiePulse.exe has made a runtime\\: in it, so a portable copy
	leaves nothing in the profile and an uninstall takes it all. The same as IndiePulse.exe's (launcher/Runtime.cs)."""
	env = dict(os.environ)
	if KIT.is_dir():
		env.setdefault("UV_CACHE_DIR", str(KIT / "data" / "uv" / "cache"))
		env.setdefault("UV_PYTHON_INSTALL_DIR", str(KIT / "data" / "uv" / "python"))
		env.setdefault("UV_PYTHON_PREFERENCE", "only-managed")
	elif RUNTIME.is_dir():
		env["UV_CACHE_DIR"] = str(uv_cache())
		env["UV_PYTHON_INSTALL_DIR"] = str(RUNTIME / "python")
		env["UV_PYTHON_PREFERENCE"] = "only-managed"
	return env


def _installed_list() -> Path:
	return LOCK if LOCK.is_file() else REQUIREMENTS


def _requirements_hash() -> str:
	"""The stamp: sha256 of requirements.txt followed by requirements.lock (IndiePulse.exe writes the same)."""
	h = hashlib.sha256(REQUIREMENTS.read_bytes())
	if LOCK.is_file():
		h.update(LOCK.read_bytes())
	return h.hexdigest()


def requirements_changed() -> bool:
	try:
		return _STAMP.read_text(encoding="ascii").strip() != _requirements_hash()
	except OSError:
		return True


def _call(cmd: list[str], log_path: Path, env: dict[str, str] | None = None) -> bool:
	"""Run hidden, output to log_path."""
	log_path.parent.mkdir(parents=True, exist_ok=True)
	with open(log_path, "w", encoding="utf-8", errors="replace") as log:
		return subprocess.call(cmd, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
							   creationflags=CREATE_NO_WINDOW) == 0


def install_requirements(log_path: Path) -> bool:
	"""requirements.lock into the venv: with uv (its cache on the drive or in runtime\\, seconds) or pip."""
	exe, listed = uv(), str(_installed_list())
	if exe:
		ok = _call([exe, "pip", "install", "--python", str(venv_python()), "-r", listed], log_path, uv_env())
		if ok:
			_drop_temp_cache()
	else:
		ok = _call([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-r", listed], log_path)
	if ok:
		_STAMP.write_text(_requirements_hash(), encoding="ascii")
	return ok


def ensure_venv(log_path: Path) -> bool:
	"""A venv that runs here: made anew when there is none or it came from another computer. With uv it is made on
	uv's Python (ClubKit's on the drive, or the runtime's; installed the first time), otherwise on the Python running
	this."""
	if venv_alive():
		return True
	home = _venv_home()
	if home and Path(home).is_dir() and not (Path(home) / "python.exe").is_file() and _repair_python(log_path) \
			and venv_alive():
		return True  # its Python is back: the venv itself was fine
	shutil.rmtree(VENV, ignore_errors=True)
	exe = uv()
	if exe:  # --seed: pip too, for when there is no uv next time; --clear: whatever rmtree couldn't remove
		cmd = [exe, "venv", "--seed", "--clear", "--python", PYTHON_VERSION, str(VENV)]
		# no Python 3.12 of uv's yet, or its python.exe is gone: installed the way IndiePulse.exe does it
		ok = _call(cmd, log_path, uv_env()) or (
			(_call([exe, *_INSTALL_PYTHON, PYTHON_VERSION], log_path, uv_env()) or _repair_python(log_path))
			and _call(cmd, log_path, uv_env()))
		_drop_temp_cache()
		return ok
	# sys.executable is pythonw under pyw.exe; venv needs a real interpreter either way
	base = Path(sys.executable)
	py = base.with_name("python.exe") if base.name.lower() == "pythonw.exe" else base
	return _call([str(py), "-m", "venv", str(VENV)], log_path)
