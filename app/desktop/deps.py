"""Keep .venv in sync with requirements.lock. Stdlib only: IndiePulse.pyw imports it
before the venv (and pystray/psutil) exist."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.desktop import KIT

ROOT = Path(__file__).resolve().parents[2]
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"  # what the app needs: edit this one
# The exact versions the app is tested with, all of them (dependencies of dependencies too): what gets installed.
# `~=` in requirements.txt alone let a fresh install take sqlmodel 0.0.48, which rejects naive datetimes.
LOCK = ROOT / "requirements.lock"
_STAMP = VENV / "indiepulse-requirements.sha256"
PYTHON_VERSION = "3.12"  # the one requirements.lock is tested with

CREATE_NO_WINDOW = 0x08000000


def venv_python(windowed: bool = False) -> Path:
	return VENV / "Scripts" / ("pythonw.exe" if windowed else "python.exe")


def venv_alive() -> bool:
	"""The .venv runs on this computer. One made on another (its Python in another user's profile, the drive under
	another letter) still has its python.exe, which only says «No Python at …»: the base Python pyvenv.cfg names
	must be here."""
	try:
		cfg = (VENV / "pyvenv.cfg").read_text(encoding="utf-8")
	except OSError:
		return False
	home = next((v.strip() for k, _, v in (line.partition("=") for line in cfg.splitlines()) if k.strip() == "home"), "")
	return bool(home) and (Path(home) / "python.exe").is_file() and venv_python().is_file()


def uv() -> str | None:
	"""uv from PATH or ClubKit's own copy on the drive — no Python needs to be installed on the computer then."""
	kit_uv = KIT / "tools" / "uv" / "uv.exe"
	return shutil.which("uv") or (str(kit_uv) if kit_uv.is_file() else None)


def uv_env() -> dict[str, str]:
	"""ClubKit keeps uv's cache and Pythons on the drive (not in the profile of a computer it will leave) and uses only
	those: a .venv made on one computer then runs on the next, if the drive has the same letter there. The same three
	as ClubKit's Get-UvEnvironment (and IndiePulse.bat)."""
	env = dict(os.environ)
	if KIT.is_dir():
		env.setdefault("UV_CACHE_DIR", str(KIT / "data" / "uv" / "cache"))
		env.setdefault("UV_PYTHON_INSTALL_DIR", str(KIT / "data" / "uv" / "python"))
		env.setdefault("UV_PYTHON_PREFERENCE", "only-managed")
	return env


def _installed_list() -> Path:
	return LOCK if LOCK.is_file() else REQUIREMENTS


def _requirements_hash() -> str:
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
	"""requirements.lock into .venv: with uv (its cache on the drive, seconds) or pip."""
	exe, listed = uv(), str(_installed_list())
	if exe:
		ok = _call([exe, "pip", "install", "--python", str(venv_python()), "-r", listed], log_path, uv_env())
	else:
		ok = _call([str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-r", listed], log_path)
	if ok:
		_STAMP.write_text(_requirements_hash(), encoding="ascii")
	return ok


def ensure_venv(log_path: Path) -> bool:
	"""A .venv that runs here: made anew when there is none or it came from another computer. With uv it is made on
	uv's Python (ClubKit's, on the drive; uv fetches it the first time), otherwise on the Python running this."""
	if venv_alive():
		return True
	shutil.rmtree(VENV, ignore_errors=True)
	exe = uv()
	if exe:  # --seed: pip too, for when there is no uv next time; --clear: whatever rmtree couldn't remove
		return _call([exe, "venv", "--seed", "--clear", "--python", PYTHON_VERSION, str(VENV)], log_path, uv_env())
	# sys.executable is pythonw under pyw.exe; venv needs a real interpreter either way
	base = Path(sys.executable)
	py = base.with_name("python.exe") if base.name.lower() == "pythonw.exe" else base
	return _call([str(py), "-m", "venv", str(VENV)], log_path)
