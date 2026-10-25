"""Build a release from a v* tag: IndiePulse-<version>.zip, latest.json and the release notes (release.yml runs it).

	python tools/build_release.py --tag v2.0.0 [--repo owner/name] [--out dist] [--exe IndiePulse.exe]
	python tools/build_release.py --tag v2.0.0 --check      # only: does the tag match app/__init__.py?

The zip is one IndiePulse/ folder with what a user sees at its top — IndiePulse.exe (built by
tools/build_launcher.ps1 first), README.md, LICENSE — and everything else in lib\\: app/, the requirements,
.env.example (app/core/paths.py: HOME and ROOT). Only files git tracks, so nothing local (a .venv, data, __pycache__)
can slip in, and no tests, .github, tools, launcher sources or dev notes. latest.json is what the updater reads
(app/services/updates.py): {version, tag, name, url, sha256, size, notes, published_at}.

Notes: the annotated tag's message (tools/release.ps1 writes them there); a lightweight tag gets the commit subjects
since the previous tag.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDER = "IndiePulse"  # the one folder inside the zip
EXE = "IndiePulse.exe"
LIB = "lib"            # the code's folder in it
TOP_FILES = {"README.md"}  # + LICENSE: next to the exe
LIB_FILES = {"requirements.txt", "requirements.lock", ".env.example"}  # + app/
_VERSION_RX = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)
_RELEASE_RX = re.compile(r"\d+\.\d+\.\d+(-rc\.\d+)?")


class BuildError(Exception):
	pass


def read_version(root: Path = ROOT) -> str:
	m = _VERSION_RX.search((root / "app" / "__init__.py").read_text(encoding="utf-8"))
	if not m:
		raise BuildError("no __version__ in app/__init__.py")
	return m.group(1)


def check_tag(tag: str, root: Path = ROOT) -> str:
	"""The version the tag releases; BuildError unless it is v + __version__ and a release version (no -dev)."""
	version = read_version(root)
	if tag != f"v{version}":
		raise BuildError(f"tag {tag} doesn't match app/__init__.py __version__ = {version!r} (expected v{version})")
	if not _RELEASE_RX.fullmatch(version):
		raise BuildError(f"{version!r} isn't a release version: X.Y.Z or X.Y.Z-rc.N")
	return version


def _git(root: Path, *args: str) -> str:
	return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True,
						  encoding="utf-8").stdout


def destination(path: str) -> str | None:
	"""Where a tracked file (posix path from the repo root) goes in the IndiePulse folder of the zip; None: not in it."""
	if path in TOP_FILES or re.fullmatch(r"LICEN[CS]E(\.\w+)?", path):
		return path
	if path in LIB_FILES or (path.startswith("app/") and "__pycache__/" not in path
							 and not path.endswith((".pyc", ".pyo"))):
		return f"{LIB}/{path}"
	return None


def included(path: str) -> bool:
	return destination(path) is not None


def release_files(root: Path = ROOT) -> list[str]:
	return sorted(p for p in _git(root, "ls-files", "-z").split("\0") if p and included(p))


def check_exe(exe: Path) -> None:
	"""IndiePulse.exe is there and is a Windows program (not, say, a git-lfs pointer or an empty file)."""
	try:
		head = Path(exe).read_bytes()[:2]
	except OSError:
		raise BuildError(f"no {exe}: build it first (powershell -File tools\\build_launcher.ps1)")
	if head != b"MZ":
		raise BuildError(f"{exe} isn't a Windows program")


def notes_for_tag(tag: str, root: Path = ROOT) -> str:
	kind, _, text = _git(root, "for-each-ref", f"refs/tags/{tag}",
						 "--format=%(objecttype)%00%(contents:subject)%0a%0a%(contents:body)").partition("\0")
	if kind.strip() == "tag" and text.strip():
		return text.strip()
	try:
		prev = _git(root, "describe", "--tags", "--abbrev=0", "--match", "v*", f"{tag}^").strip()
		span = [f"{prev}..{tag}"]
	except subprocess.CalledProcessError:
		span = [tag, "-n", "30"]  # the first release
	# without the version commits tools/release.ps1 makes
	return _git(root, "log", "--no-merges", "--invert-grep", "--grep=^Start [0-9.]*-dev$", "--grep=^Release [0-9]",
				"--pretty=format:- %s", *span).strip()


def _sha256(path: Path) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			h.update(chunk)
	return h.hexdigest()


def _add(z: zipfile.ZipFile, arcname: str, src: Path) -> None:
	info = zipfile.ZipInfo(f"{FOLDER}/{arcname}", time.localtime(os.path.getmtime(src))[:6])
	info.external_attr = 0o644 << 16
	z.writestr(info, src.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)


def build(tag: str, out: Path, *, repo: str, notes: str, exe: Path, root: Path = ROOT,
		  files: list[str] | None = None, published_at: str | None = None) -> dict:
	version = check_tag(tag, root)
	files = release_files(root) if files is None else files
	if "app/__init__.py" not in files or "requirements.lock" not in files:
		raise BuildError("the file list misses app/__init__.py or requirements.lock")
	check_exe(exe)
	out.mkdir(parents=True, exist_ok=True)
	name = f"IndiePulse-{version}.zip"
	zpath = out / name
	with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
		_add(z, EXE, Path(exe))
		for rel in files:
			_add(z, destination(rel) or rel, root / rel)
	sha = _sha256(zpath)
	manifest = {
		"version": version,
		"tag": tag,
		"name": name,
		"url": f"https://github.com/{repo}/releases/download/{tag}/{name}",
		"sha256": sha,
		"size": zpath.stat().st_size,
		"notes": notes,
		"published_at": published_at or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
	}
	(out / "latest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
	# SignPath Foundation wants the code signing policy named on the download page too
	(out / "notes.md").write_text(f"{notes}\n\nSHA-256 of {name}: `{sha}`\n\n"
								  f"[Code signing policy](https://github.com/{repo}#code-signing-policy) · "
								  f"[Privacy policy](https://github.com/{repo}#privacy-policy)\n", encoding="utf-8")
	return manifest


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	ap.add_argument("--tag", default=os.getenv("GITHUB_REF_NAME", ""))
	ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "Sargonius/IndiePulseApp"))
	ap.add_argument("--out", type=Path, default=ROOT / "dist")
	ap.add_argument("--exe", type=Path, default=ROOT / EXE, help="IndiePulse.exe (tools/build_launcher.ps1)")
	ap.add_argument("--check", action="store_true", help="only check the tag against __version__")
	args = ap.parse_args(argv)
	try:
		version = check_tag(args.tag)
		if not args.check:
			manifest = build(args.tag, args.out, repo=args.repo, notes=notes_for_tag(args.tag), exe=args.exe)
			print(json.dumps({k: v for k, v in manifest.items() if k != "notes"}, indent=2))
	except BuildError as exc:
		print(f"::error::{exc}")
		return 1
	gh_out = os.getenv("GITHUB_OUTPUT")
	if gh_out:  # for the workflow's next steps
		with open(gh_out, "a", encoding="utf-8") as f:
			f.write(f"version={version}\nprerelease={'true' if '-' in version else 'false'}\n")
	print(f"{args.tag}: IndiePulse {version}")
	return 0


if __name__ == "__main__":
	sys.exit(main())
