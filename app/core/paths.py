"""Paths saved on one computer, read on another: the drive IndiePulse is on goes from computer to computer and isn't
always under the same letter (E: at home once, F: now), while settings and the DB keep absolute paths on it."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


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
