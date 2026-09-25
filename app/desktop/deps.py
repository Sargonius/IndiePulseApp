"""Keep .venv in sync with requirements.txt. Stdlib only: IndiePulse.pyw imports it
before the venv (and pystray/psutil) exist."""
from __future__ import annotations

import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
_STAMP = VENV / "indiepulse-requirements.sha256"

CREATE_NO_WINDOW = 0x08000000


def venv_python(windowed: bool = False) -> Path:
	return VENV / "Scripts" / ("pythonw.exe" if windowed else "python.exe")


def _requirements_hash() -> str:
	return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def requirements_changed() -> bool:
	try:
		return _STAMP.read_text(encoding="ascii").strip() != _requirements_hash()
	except OSError:
		return True


def install_requirements(log_path: Path) -> bool:
	"""pip install -r requirements.txt into .venv, hidden, output to log_path."""
	log_path.parent.mkdir(parents=True, exist_ok=True)
	with open(log_path, "w", encoding="utf-8", errors="replace") as log:
		rc = subprocess.call(
			[str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check", "-r", str(REQUIREMENTS)],
			cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
			creationflags=CREATE_NO_WINDOW,
		)
	if rc == 0:
		_STAMP.write_text(_requirements_hash(), encoding="ascii")
	return rc == 0


def ensure_venv() -> None:
	if not venv_python().exists():
		# sys.executable is pythonw under pyw.exe; venv needs a real interpreter either way
		base = Path(sys.executable)
		py = base.with_name("python.exe") if base.name.lower() == "pythonw.exe" else base
		subprocess.check_call([str(py), "-m", "venv", str(VENV)], cwd=ROOT, creationflags=CREATE_NO_WINDOW)
