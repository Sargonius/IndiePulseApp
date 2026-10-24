"""Installs a downloaded IndiePulse release while nothing of IndiePulse runs, and takes it back if it doesn't start.

Stdlib only, and run as a copy (the tray copies this file into the updates folder and starts it with -I -S): it must
not depend on the code it replaces. The tray (app/desktop/updater.py) has already downloaded the zip, checked its
sha256 and unpacked it; it starts this, stops the server and the worker, and quits. Then:

1. wait for the tray to exit. From here on no IndiePulse process holds anything: not the .py files, and not the
   venv's packages either — the tray itself imports pystray, psutil and Pillow, whose .pyd files Windows won't let
   an install replace while they are loaded. That is why this is a separate process and not a step of the tray;
2. swap: every top-level name of the release — IndiePulse.exe, README.md, LICENSE and lib\\ (the code) — is moved out
   of IndiePulse's folder (HOME, app/core/paths.py) into previous/, the new one moved in. Renames within one drive
   (updates.py picks the folder so), so each is all at once; a locked file (an antivirus scan) is retried, and if a
   move still fails, everything moved so far goes back. What isn't in the release stays as it is: runtime\\ (uv,
   Python, the venv), portable.txt, data\\ — the data (DATA) is never touched;
3. requirements.lock of the new code into the venv (app/desktop/deps.py of that code, in a child process);
4. start the new tray (`pythonw -m app.desktop.tray --background` in lib\\) and wait for its server: /health, and
   /version saying the new version. Not within HEALTH_TIMEOUT, or the tray exits → roll back: kill it, move the new
   code aside into failed/, previous/ back, the old requirements, start the old tray. progress.json says how it went
   (the banner and the tray read it); details in logs/update.log.

While this runs it holds a named mutex IndiePulse.exe looks for (updating_mutex_name): a double-click in the middle
waits for it instead of starting a tray of its own. The update folder keeps previous/ (the version before) until the
next update.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HEALTH_TIMEOUT = 240   # s from starting the new tray to its server answering (it may install packages first)
TRAY_EXIT_TIMEOUT = 300  # s for the old tray to stop the worker (which finishes its task) and quit
# Never replaced by an update, whatever a release holds: the environment, git, the user's settings and data
PROTECTED = {"runtime", ".venv", "venv", ".git", ".env", "portable.txt", "data", "updates", ".updates", "logs", "media",
			 "render", "cache", "tmp", "vendor", ".locks", "browser", ".settings.json", ".draft.json",
			 ".draft_news.json", ".autoreload"}
_PROTECTED_SUFFIXES = (".db", ".db-wal", ".db-shm", ".db-journal", ".log")
EXE = "IndiePulse.exe"
LIB = "lib"  # a release's code folder (app/core/paths.py: ROOT)
REQUIRED = (EXE, "lib/app/__init__.py", "lib/app/main.py", "lib/requirements.lock")
_VERSION_RX = re.compile(r'^__version__\s*=\s*"([^"]+)"', re.M)

CREATE_NO_WINDOW = 0x08000000
_DETACHED = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
_BREAKAWAY = 0x01000000              # CREATE_BREAKAWAY_FROM_JOB: outlive a job the parent may be in
_ERROR_NOT_SAME_DEVICE = 17


class SwapError(Exception):
	pass


# ── small shared helpers (app/services/updates.py uses them too) ─────────────
def read_json(path: Path) -> dict:
	try:
		data = json.loads(Path(path).read_text(encoding="utf-8"))
	except (OSError, ValueError):
		return {}
	return data if isinstance(data, dict) else {}


def write_json(path: Path, data: dict) -> None:
	"""Write-then-rename: a reader (the banner polls) never sees half a file."""
	path = Path(path)
	path.parent.mkdir(parents=True, exist_ok=True)
	tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
	tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
	for attempt in range(40):
		try:
			os.replace(tmp, path)
			return
		except PermissionError:  # a reader or an antivirus has it open this instant
			if attempt == 39:
				raise
			time.sleep(0.05)


def set_progress(work: Path, phase: str, version: str = "", text: str = "", **extra) -> dict:
	data = {"phase": phase, "version": version, "text": text, "at": int(time.time()), **extra}
	write_json(Path(work) / "progress.json", data)
	return data


def code_dir(home: Path) -> Path:
	"""Where app/ is in IndiePulse's folder: lib\\ of a release, the folder itself in a checkout (paths.ROOT)."""
	home = Path(home)
	return home / LIB if (home / LIB / "app" / "__init__.py").is_file() else home


def version_of(home: Path) -> str:
	try:
		m = _VERSION_RX.search((code_dir(home) / "app" / "__init__.py").read_text(encoding="utf-8"))
	except OSError:
		return ""
	return m.group(1) if m else ""


def instance_key(home: Path) -> str:
	"""What the named events and mutexes of one IndiePulse folder are called by. IndiePulse.exe passes its own
	(INDIEPULSE_INSTANCE; launcher/Home.cs: sha1 of the folder's full path in lower case), and the tray's children
	inherit it; this is the same, for a tray started without the exe."""
	return os.getenv("INDIEPULSE_INSTANCE") or \
		hashlib.sha1(str(Path(home).resolve()).lower().encode("utf-8")).hexdigest()[:16]


def updating_mutex_name(home: Path) -> str:
	return f"Local\\IndiePulse-{instance_key(home)}-updating"


def find_release_root(staging: Path) -> Path:
	"""The unpacked release: `staging` itself or its one folder (the zip has IndiePulse/ inside)."""
	staging = Path(staging)
	if version_of(staging):
		return staging
	entries = list(staging.iterdir())
	if len(entries) == 1 and entries[0].is_dir() and version_of(entries[0]):
		return entries[0]
	raise SwapError("the zip has no IndiePulse folder with lib/app/ in it")


def release_items(new: Path) -> list[str]:
	"""The top-level names a release replaces; refuses one that holds anything of PROTECTED."""
	names = sorted(p.name for p in Path(new).iterdir())
	bad = [n for n in names if n.lower() in PROTECTED or n.lower().endswith(_PROTECTED_SUFFIXES)]
	if bad:
		raise SwapError(f"the release holds {', '.join(bad)}, which an update never replaces")
	missing = [r for r in REQUIRED if not (Path(new) / r).is_file()]
	if missing:
		raise SwapError(f"the release misses {', '.join(missing)}")
	return names


# ── the swap ─────────────────────────────────────────────────────────────────
def _move(src: Path, dst: Path, attempts: int = 40) -> None:
	"""A rename; retried while something has a file open (an antivirus or the indexer looking at a new file)."""
	for attempt in range(attempts):
		try:
			os.rename(src, dst)
			return
		except PermissionError:
			if attempt == attempts - 1:
				raise
			time.sleep(0.25)
		except OSError as exc:
			if getattr(exc, "winerror", None) == _ERROR_NOT_SAME_DEVICE:
				shutil.move(str(src), str(dst))  # another drive after all: a copy, then a delete
				return
			raise


def _clear(path: Path) -> None:
	if path.exists():
		shutil.rmtree(path)


def swap(root: Path, new: Path, backup: Path) -> dict:
	"""The release in `new` in place of the same names in `root`; what they were goes into `backup` (emptied first).
	All or nothing: if a move fails, whatever was moved goes back and SwapError says why. Returns the record restore()
	needs, also kept in backup/previous.json."""
	root, new, backup = Path(root), Path(new), Path(backup)
	names = release_items(new)
	record = {"version": version_of(root), "replaced": [], "added": [], "at": int(time.time())}
	_clear(backup)
	backup.mkdir(parents=True)
	done: list[tuple[str, bool]] = []
	try:
		for name in names:
			had_old = os.path.lexists(root / name)
			if had_old:
				_move(root / name, backup / name)
			try:
				_move(new / name, root / name)
			except Exception:
				if had_old:
					_move(backup / name, root / name)
				raise
			done.append((name, had_old))
	except Exception as exc:
		stuck = []
		for name, had_old in reversed(done):
			try:
				_move(root / name, new / name)
				if had_old:
					_move(backup / name, root / name)
			except Exception as undo_exc:
				stuck.append(f"{name} ({undo_exc})")
		if stuck:
			raise SwapError(f"couldn't replace the files ({exc}), and couldn't put back {', '.join(stuck)}: "
							f"copy them back from {backup} by hand") from exc
		raise SwapError(f"couldn't replace the files ({exc}); nothing was changed") from exc
	record["replaced"] = [n for n, had in done if had]
	record["added"] = [n for n, had in done if not had]
	write_json(backup / "previous.json", record)
	return record


def restore(root: Path, backup: Path, aside: Path) -> dict:
	"""Undo a swap(): the new code into `aside` (emptied first; kept for a look), the old one back from `backup`."""
	root, backup, aside = Path(root), Path(backup), Path(aside)
	record = read_json(backup / "previous.json")
	if not record:
		raise SwapError(f"nothing to roll back to: no {backup / 'previous.json'}")
	_clear(aside)
	aside.mkdir(parents=True)
	for name in record.get("replaced", []) + record.get("added", []):
		if os.path.lexists(root / name):
			_move(root / name, aside / name)
	for name in record.get("replaced", []):
		_move(backup / name, root / name)
	(backup / "previous.json").unlink(missing_ok=True)  # previous/ is empty now: the old code is back in place
	return record


# ── processes ────────────────────────────────────────────────────────────────
def _popen_detached(cmd: list[str], cwd: Path) -> subprocess.Popen:
	kw = dict(cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
			  close_fds=True)
	if os.name != "nt":
		return subprocess.Popen(cmd, start_new_session=True, **kw)
	try:
		return subprocess.Popen(cmd, creationflags=_DETACHED | _BREAKAWAY, **kw)
	except OSError:  # the job we're in doesn't allow breaking away
		return subprocess.Popen(cmd, creationflags=_DETACHED, **kw)


def launch(*, python: Path, work: Path, root: Path, data: Path, staged: Path, version: str, from_version: str,
		   pid: int, url: str) -> subprocess.Popen:
	"""Start this file as the installer (from the tray, before it stops everything). A copy in `work`: the original is
	in app/, which the swap moves."""
	work = Path(work)
	work.mkdir(parents=True, exist_ok=True)
	script = work / "apply_update.py"
	shutil.copyfile(__file__, script)
	cmd = [str(python), "-I", "-S", str(script), "--root", str(root), "--data", str(data), "--work", str(work),
		   "--staged", str(staged), "--version", version, "--from-version", from_version, "--pid", str(pid),
		   "--url", url]
	return _popen_detached(cmd, work)


def wait_for_exit(pid: int, timeout: float) -> bool:
	"""True once process `pid` is gone (False: still there after `timeout`)."""
	if os.name != "nt":
		deadline = time.monotonic() + timeout
		while time.monotonic() < deadline:
			try:
				os.kill(pid, 0)
			except OSError:
				return True
			time.sleep(0.5)
		return False
	import ctypes
	k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	k32.OpenProcess.restype = ctypes.c_void_p
	handle = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
	if not handle:
		return True  # already gone
	try:
		return k32.WaitForSingleObject(ctypes.c_void_p(handle), int(timeout * 1000)) == 0
	finally:
		k32.CloseHandle(ctypes.c_void_p(handle))


def kill_tree(pid: int) -> None:
	if os.name == "nt":
		subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
					   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
	else:
		try:
			os.kill(pid, 9)
		except OSError:
			pass


def venv_python(home: Path, windowed: bool = False) -> Path:
	"""The venv's Python (deps.VENV): runtime\venv of a release, .venv of a checkout."""
	home = Path(home)
	venv = home / "runtime" / "venv" if code_dir(home) != home else home / ".venv"
	return venv / "Scripts" / ("pythonw.exe" if windowed else "python.exe")


# deps.py of whatever code is in home now: the new code's requirements after the swap, the old ones after a rollback
_SYNC = ("import sys; sys.path.insert(0, sys.argv[1]); from pathlib import Path; from app.desktop import deps; "
		 "sys.exit(0 if not deps.requirements_changed() or deps.install_requirements(Path(sys.argv[2])) else 1)")


def sync_requirements(home: Path, log_path: Path) -> bool:
	python, code = venv_python(home), code_dir(home)
	if not python.is_file():
		return True  # nothing to sync into; IndiePulse.exe makes the venv when it starts
	try:
		return subprocess.run([str(python), "-I", "-S", "-c", _SYNC, str(code), str(log_path)], cwd=str(code),
							  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
							  creationflags=CREATE_NO_WINDOW if os.name == "nt" else 0, timeout=1200).returncode == 0
	except (OSError, subprocess.TimeoutExpired):
		return False


def start_tray(home: Path) -> subprocess.Popen:
	"""The tray of whatever code is in home now, without opening a window (an open one reloads itself). It gets this
	process's environment — the old tray's, INDIEPULSE_INSTANCE with it — so IndiePulse.exe finds the new one too."""
	return _popen_detached([str(venv_python(home, windowed=True)), "-m", "app.desktop.tray", "--background"],
						   code_dir(home))


def hold_updating_mutex(home: Path):
	"""The mutex IndiePulse.exe waits on while an update runs, held until this process ends (None off Windows)."""
	if os.name != "nt":
		return None
	import ctypes
	k32 = ctypes.WinDLL("kernel32", use_last_error=True)
	k32.CreateMutexW.restype = ctypes.c_void_p
	k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
	return k32.CreateMutexW(None, True, updating_mutex_name(home))


def _get_json(url: str):
	try:
		with urllib.request.urlopen(url, timeout=3) as r:
			return r.status, json.loads(r.read().decode("utf-8") or "null")
	except Exception:
		return 0, None


def wait_healthy(url: str, version: str, proc: subprocess.Popen, timeout: float = HEALTH_TIMEOUT,
				 interval: float = 2.0) -> tuple[bool, str]:
	"""(True, "") once the server answers /health and its /version is `version`; (False, why) otherwise."""
	base = url.rstrip("/")
	deadline = time.monotonic() + timeout
	seen = ""
	while time.monotonic() < deadline:
		if proc.poll() is not None:
			return False, f"it exited at start, code {proc.returncode}"
		status, _ = _get_json(base + "/health")
		if status == 200:
			vstatus, body = _get_json(base + "/version")
			seen = str((body or {}).get("app_version") or "") if isinstance(body, dict) else ""
			if vstatus != 200 or seen in ("", version):
				return True, ""
		time.sleep(interval)
	if seen:
		return False, f"the server answering is {seen}, not {version}"
	return False, f"its server didn't answer in {int(timeout)} s"


# ── the installer ────────────────────────────────────────────────────────────
def _clean(work: Path) -> None:
	"""The download and its unpacked folder (emptied by the swap): done with, either way."""
	shutil.rmtree(work / "staging", ignore_errors=True)
	for done in [work / "ready.json", *work.glob("IndiePulse-*.zip")]:
		done.unlink(missing_ok=True)


class _Log:
	def __init__(self, path: Path):
		self.path = Path(path)
		self.path.parent.mkdir(parents=True, exist_ok=True)

	def __call__(self, text: str) -> None:
		with open(self.path, "a", encoding="utf-8") as f:
			f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} | {text}\n")


def run(args: argparse.Namespace) -> int:
	root, work, data = Path(args.root), Path(args.work), Path(args.data)
	log = _Log(data / "logs" / "update.log")
	new, old = args.version, args.from_version
	log(f"installing {new} over {old} in {root}")
	set_progress(work, "installing", new, f"Installing IndiePulse {new}…", from_version=old)
	_mutex = hold_updating_mutex(root)  # noqa: F841  (held: a double-click on IndiePulse.exe waits for this run)
	if not wait_for_exit(args.pid, TRAY_EXIT_TIMEOUT):
		log(f"the tray (pid {args.pid}) is still running after {TRAY_EXIT_TIMEOUT} s: killing it")
		kill_tree(args.pid)
		wait_for_exit(args.pid, 10)
	time.sleep(1)  # let Windows close the handles of the processes that just ended
	try:
		swap(root, Path(args.staged), work / "previous")
	except Exception as exc:
		log(f"swap failed: {exc}")
		set_progress(work, "failed", new, f"Couldn't install IndiePulse {new}: {exc}. Still on {old}.",
					 from_version=old)
		start_tray(root)
		return 1
	log("files replaced; installing requirements")
	pip_log = data / "logs" / "pip.log"
	proc = None
	if sync_requirements(root, pip_log):
		log("starting the new tray")
		proc = start_tray(root)
		ok, why = wait_healthy(args.url, new, proc)
	else:
		ok, why = False, "its packages didn't install (logs\\pip.log)"
	if ok:
		log(f"{new} is up")
		set_progress(work, "installed", new, f"IndiePulse updated to {new}.", from_version=old)
		_clean(work)
		return 0
	log(f"rolling back to {old}: {why}")
	if proc is not None:
		kill_tree(proc.pid)
		wait_for_exit(proc.pid, 15)
		time.sleep(2)  # the server's port
	try:
		restore(root, work / "previous", work / "failed")
	except Exception as exc:
		log(f"ROLLBACK FAILED: {exc}")
		set_progress(work, "failed", new, f"IndiePulse {new} didn't start ({why}), and going back to {old} failed: "
						  f"{exc}. See logs\\update.log.", from_version=old)
		return 2
	_clean(work)  # the new code itself is in failed/
	if not sync_requirements(root, pip_log):
		log("the old requirements didn't install; the tray will try again")
	# progress first: the old tray reads it when it starts
	set_progress(work, "rolled_back", new, f"IndiePulse {new} didn't start ({why}), so {old} is back. "
				 f"Details: logs\\update.log.", from_version=old)
	start_tray(root)
	log(f"back on {old}")
	return 1


def main(argv: list[str] | None = None) -> int:
	ap = argparse.ArgumentParser(description="Install a downloaded IndiePulse release (started by the tray).")
	for name in ("root", "data", "work", "staged", "version", "from-version", "url"):
		ap.add_argument(f"--{name}", required=True)
	ap.add_argument("--pid", type=int, required=True)
	args = ap.parse_args(argv)
	try:
		return run(args)
	except Exception as exc:  # last resort: say so where the banner and the tray look
		try:
			_Log(Path(args.data) / "logs" / "update.log")(f"installer crashed: {exc!r}")
			set_progress(Path(args.work), "failed", args.version, f"The installer crashed: {exc}. "
						 f"See logs\\update.log.", from_version=args.from_version)
		finally:
			start_tray(Path(args.root))
		return 3


if __name__ == "__main__":
	sys.exit(main())
