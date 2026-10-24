"""Build a release from a v* tag: IndiePulse-<version>.zip, latest.json and the release notes (release.yml runs it).

	python tools/build_release.py --tag v2.0.0 [--repo owner/name] [--out dist]
	python tools/build_release.py --tag v2.0.0 --check      # only: does the tag match app/__init__.py?

The zip holds what a user needs to run IndiePulse — app/, the launchers, the requirements, README, LICENSE — under
one IndiePulse/ folder; only files git tracks, so nothing local (a .venv, data, __pycache__) can slip in, and no
tests, .github, tools or dev notes. latest.json is what the updater reads (app/services/updates.py):
{version, tag, name, url, sha256, size, notes, published_at}.

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
TOP_FILES = {"IndiePulse.pyw", "IndiePulse.bat", "requirements.txt", "requirements.lock", "README.md", ".env.example"}
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


def included(path: str) -> bool:
	"""Whether a tracked file (posix path from the repo root) goes into the zip."""
	if path in TOP_FILES or re.fullmatch(r"LICEN[CS]E(\.\w+)?", path):
		return True
	return path.startswith("app/") and "__pycache__/" not in path and not path.endswith((".pyc", ".pyo"))


def release_files(root: Path = ROOT) -> list[str]:
	return sorted(p for p in _git(root, "ls-files", "-z").split("\0") if p and included(p))


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
	return _git(root, "log", "--no-merges", "--pretty=format:- %s", *span).strip()


def _sha256(path: Path) -> str:
	h = hashlib.sha256()
	with open(path, "rb") as f:
		for chunk in iter(lambda: f.read(1 << 20), b""):
			h.update(chunk)
	return h.hexdigest()


def build(tag: str, out: Path, *, repo: str, notes: str, root: Path = ROOT, files: list[str] | None = None,
		  published_at: str | None = None) -> dict:
	version = check_tag(tag, root)
	files = release_files(root) if files is None else files
	if "app/__init__.py" not in files or "IndiePulse.pyw" not in files:
		raise BuildError("the file list misses app/__init__.py or IndiePulse.pyw")
	out.mkdir(parents=True, exist_ok=True)
	name = f"IndiePulse-{version}.zip"
	zpath = out / name
	with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
		for rel in files:
			data = (root / rel).read_bytes()
			if rel.lower().endswith((".bat", ".cmd")):  # cmd.exe misreads LF-only batch files
				data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
			info = zipfile.ZipInfo(f"{FOLDER}/{rel}", time.localtime(os.path.getmtime(root / rel))[:6])
			info.external_attr = 0o644 << 16
			z.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED)
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
	(out / "notes.md").write_text(f"{notes}\n\nSHA-256 of {name}: `{sha}`\n", encoding="utf-8")
	return manifest


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	ap.add_argument("--tag", default=os.getenv("GITHUB_REF_NAME", ""))
	ap.add_argument("--repo", default=os.getenv("GITHUB_REPOSITORY", "Sargonius/IndiePulseNew"))
	ap.add_argument("--out", type=Path, default=ROOT / "dist")
	ap.add_argument("--check", action="store_true", help="only check the tag against __version__")
	args = ap.parse_args(argv)
	try:
		version = check_tag(args.tag)
		if not args.check:
			manifest = build(args.tag, args.out, repo=args.repo, notes=notes_for_tag(args.tag))
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
