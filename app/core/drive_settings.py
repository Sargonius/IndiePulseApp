"""IndiePulse.portable.ini: optional settings of the drive IndiePulse is carried on. Stdlib only: app/core/paths.py
reads it before anything else. IndiePulse.exe (launcher/DriveSettings.cs) reads the same file the same way.

Where: next to IndiePulse.exe (HOME), else in the root of its drive; the first one found counts, none: the defaults.
Lines of `key = value`, keys in any case; a line that starts with # or ; is a comment; UTF-8 with or without a BOM.
Every key is optional:

- portable = yes|no: the copy is portable, as with portable.txt (data\\ next to IndiePulse.exe), and the setup
  preselects Portable;
- uv = <uv.exe>: the uv to use, before the one IndiePulse.exe downloads into runtime\\;
- uv_cache_dir = <folder>, uv_python_dir = <folder>: where uv keeps its cache and its Pythons (UV_CACHE_DIR,
  UV_PYTHON_INSTALL_DIR) instead of runtime\\, unless the environment already says where;
- trusted_pcs = <file>: the computers that may get shortcuts and the autostart entry, a «NAME|MachineGuid» line
  each (app/desktop/winapi.py); set, no other computer gets them.

A path: %VARS% expanded; \\folder from the root of the drive the ini is on, whatever its letter today; folder or
..\\folder from the ini's folder; X:\\folder as written. Unknown keys and lines that aren't `key = value` are left out
and kept in `problems`, which the tray logs.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

NAME = "IndiePulse.portable.ini"
_PATHS = ("uv", "uv_cache_dir", "uv_python_dir", "trusted_pcs")
KEYS = ("portable", *_PATHS)
_YES, _NO = ("yes", "true", "on", "1"), ("no", "false", "off", "0")
_VAR = re.compile(r"%([^%]+)%")
# what a Windows path can't have: .NET's Path.GetInvalidPathChars(), which IndiePulse.exe checks
_BAD = set('"<>|') | {chr(c) for c in range(32)}


class DriveSettings:
	"""What an ini says: `portable`, and full paths (None: not set). `file` None: there is no ini, all defaults."""

	def __init__(self, file: Path | None = None) -> None:
		self.file = file
		self.portable = False
		self.uv: Path | None = None
		self.uv_cache_dir: Path | None = None
		self.uv_python_dir: Path | None = None
		self.trusted_pcs: Path | None = None
		self.problems: list[str] = []

	def __repr__(self) -> str:
		return f"DriveSettings({self.file}, portable={self.portable}, " + ", ".join(
			f"{k}={getattr(self, k)}" for k in _PATHS) + ")"


def _drive_root(path: Path) -> Path:
	return Path(path.anchor)


def find(home: Path) -> Path | None:
	"""The ini that counts for the IndiePulse folder `home`: next to IndiePulse.exe, else in the root of its drive."""
	for ini in (home / NAME, _drive_root(home) / NAME):
		if ini.is_file():
			return ini
	return None


def load(home: Path) -> DriveSettings:
	ini = find(home)
	return read(ini) if ini else DriveSettings()


def _unquote(value: str) -> str:
	return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def _path(value: str, ini: Path) -> Path | None:
	"""A path as the ini writes it, made full (module docstring); None when it can't be a path."""
	value = _VAR.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value).replace("/", "\\")
	if any(c in _BAD for c in value):
		return None
	if value.startswith("\\") and not value.startswith("\\\\"):
		full = os.path.join(str(_drive_root(ini)), value.lstrip("\\"))
	else:  # X:\... and \\server\... stay as they are
		full = os.path.join(str(ini.parent), value)
	return Path(os.path.abspath(full))


def read(ini: Path) -> DriveSettings:
	s = DriveSettings(ini)
	try:
		text = ini.read_text(encoding="utf-8-sig", errors="replace")
	except OSError as exc:
		s.problems.append(f"couldn't be read ({exc}): the defaults")
		return s
	for n, raw in enumerate(text.split("\n"), 1):
		line = raw.strip()
		if not line or line[0] in "#;":
			continue
		key, eq, value = line.partition("=")
		key, value = key.strip().lower(), _unquote(value.strip())
		if not eq or not key:
			s.problems.append(f"line {n} isn't «key = value», left out: {line}")
		elif key not in KEYS:
			s.problems.append(f"line {n}: unknown key {key!r}, left out")
		elif key == "portable":
			if value.lower() in _YES + _NO:
				s.portable = value.lower() in _YES
			else:
				s.problems.append(f"line {n}: portable is yes or no, not {value!r}; left out")
		elif not value:
			setattr(s, key, None)  # `uv =`: not set, even if a line above set it
		else:
			path = _path(value, ini)
			setattr(s, key, path)
			if path is None:
				s.problems.append(f"line {n}: {value!r} isn't a path, left out")
	return s
