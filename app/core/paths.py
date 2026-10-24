"""Where IndiePulse keeps its code and its data. Stdlib only: IndiePulse.pyw imports it before the venv exists.

The data — the DBs, media/, render/, logs/, .settings.json, vendor/ffmpeg — is opened by paths relative to the
working directory, and every process (tray, server, worker) makes DATA its working directory first
(enter_data_dir). The code finds its own files (templates, static) from ROOT. So the code can be replaced by an
update without touching the data, and the data can live elsewhere than the code:

- INDIEPULSE_DATA set: there (tests, a second instance);
- a copy that has been running from its own folder (its DB or settings are there): that folder, nothing moves;
- portable (portable.txt next to the app, or ClubKit on the drive): data/ next to the app;
- installed: %LOCALAPPDATA%\\IndiePulse.

Also: paths saved on one computer, read on another. The drive IndiePulse is on goes from computer to computer and
isn't always under the same letter (E: at home once, F: now), while settings and the DB keep absolute paths on it.
"""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEMPLATES_DIR = str(ROOT / "app" / "templates")


def _data_dir() -> Path:
	env = os.getenv("INDIEPULSE_DATA")
	if env:
		return Path(env).resolve()
	if (ROOT / "indiepulse.db").exists() or (ROOT / ".settings.json").exists():
		return ROOT
	if (ROOT / "portable.txt").exists() or (Path(ROOT.anchor) / "ClubKit").is_dir():
		return ROOT / "data"
	local = os.getenv("LOCALAPPDATA")
	return Path(local) / "IndiePulse" if local else ROOT / "data"


DATA = _data_dir()


def enter_data_dir() -> Path:
	"""DATA as the working directory (made if needed): the app opens its data by relative paths."""
	DATA.mkdir(parents=True, exist_ok=True)
	os.chdir(DATA)
	return DATA


def on_this_drive(path: str) -> str:
	"""`path` as is when it exists or isn't on a lettered drive; otherwise the same path on IndiePulse's own drive, when
	it is there (E:\\SynthSocial\\Music saved at home → F:\\SynthSocial\\Music in the club)."""
	if not path or os.path.exists(path):
		return path
	drive, rest = os.path.splitdrive(path)
	here = os.path.splitdrive(str(ROOT))[0]
	if len(drive) == 2 and drive[1] == ":" and here and drive.upper() != here.upper():
		moved = here + rest
		if os.path.exists(moved):
			return moved
	return path
