"""Where IndiePulse keeps its code, its runtime and its data. Stdlib only: app/desktop/launch.py imports it before the
venv exists. IndiePulse.exe (launcher/Home.cs) answers the same questions the same way.

The folders:
- ROOT, the code: the folder app/ is in. A release has it in lib\\ — the zip is IndiePulse.exe, README.md, LICENSE and
  lib\\ — a checkout is its own root;
- HOME, what the user sees: the folder with IndiePulse.exe. lib\\'s parent in a release, ROOT in a checkout. A portable
  copy keeps portable.txt and data\\ here, and a release its runtime\\ (uv, Python, the venv: IndiePulse.exe prepares
  it). An update replaces the exe, README, LICENSE and lib\\, nothing else;
- DATA, the data — the DBs, media/, render/, logs/, .settings.json, vendor/ffmpeg, the app window's browser profile.

The data is opened by paths relative to the working directory, and every process (tray, server, worker) makes DATA
its working directory first (enter_data_dir). The code finds its own files (templates, static) from ROOT. So the code
can be replaced by an update without touching the data, and the data can live elsewhere than the code:

- INDIEPULSE_DATA set: there (tests, a second instance);
- a copy that has been running from its own folder (its DB or settings are there): that folder, nothing moves;
- portable (portable.txt next to IndiePulse.exe, or `portable = yes` in the drive settings, DRIVE): data\\ next to
  IndiePulse.exe;
- installed: %LOCALAPPDATA%\\IndiePulse.

Also: paths saved on one computer, read on another. The drive IndiePulse is on goes from computer to computer and
isn't always under the same letter (E: at home once, F: now), while settings and the DB keep absolute paths on it.
"""
from __future__ import annotations

import os
from pathlib import Path

from app.core import drive_settings

ROOT = Path(__file__).resolve().parents[2]
# lib\ of a release (a checkout that happens to be called lib has its .git)
HOME = ROOT.parent if ROOT.name.lower() == "lib" and not (ROOT / ".git").exists() else ROOT
RUNTIME = HOME / "runtime"
# IndiePulse.portable.ini next to IndiePulse.exe or in the root of its drive: portable, its uv and uv's folders, the
# trusted computers (app/core/drive_settings.py)
DRIVE = drive_settings.load(HOME)
TEMPLATES_DIR = str(ROOT / "app" / "templates")


def is_release_layout() -> bool:
	"""The code in lib\\ under IndiePulse.exe's folder (a release), not a checkout (HOME is ROOT)."""
	return HOME != ROOT


def _data_dir() -> Path:
	env = os.getenv("INDIEPULSE_DATA")
	if env:
		return Path(env).resolve()
	if (ROOT / "indiepulse.db").exists() or (ROOT / ".settings.json").exists():
		return ROOT
	if (HOME / "portable.txt").exists() or DRIVE.portable:
		return HOME / "data"
	local = os.getenv("LOCALAPPDATA")
	return Path(local) / "IndiePulse" if local else HOME / "data"


DATA = _data_dir()


def enter_data_dir() -> Path:
	"""DATA as the working directory (made if needed): the app opens its data by relative paths."""
	DATA.mkdir(parents=True, exist_ok=True)
	os.chdir(DATA)
	return DATA


def on_this_drive(path: str) -> str:
	"""`path` as is when it exists or isn't on a lettered drive; otherwise the same path on IndiePulse's own drive, when
	it is there (E:\\Music saved at home → F:\\Music on a computer where the drive got the letter F:)."""
	if not path or os.path.exists(path):
		return path
	drive, rest = os.path.splitdrive(path)
	here = os.path.splitdrive(str(ROOT))[0]
	if len(drive) == 2 and drive[1] == ":" and here and drive.upper() != here.upper():
		moved = here + rest
		if os.path.exists(moved):
			return moved
	return path
