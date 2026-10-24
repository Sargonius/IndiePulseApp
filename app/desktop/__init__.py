"""Desktop shell: tray icon + supervisor that runs the web server and the Huey worker.

What users start is IndiePulse.exe (launcher/: the first run is its setup, Install or Portable; it prepares Python
and the venv, then runs `pythonw -m app.desktop.tray`). A checkout without the exe built starts with
`python -m app.desktop.launch` (or `python -m app.desktop.tray` when its .venv is in place).
"""
import os
from pathlib import Path

from app.core.paths import DATA, HOME, ROOT, RUNTIME, is_release_layout  # noqa: F401  (re-exported)
# ClubKit: the user's kit for working from other people's computers, in the root of the drive IndiePulse is on.
# IndiePulse takes its uv (and the Pythons uv keeps there) and its list of trusted computers; without it IndiePulse
# behaves as an installed program.
KIT = Path(ROOT.anchor) / "ClubKit"
PORTABLE_MARKER = HOME / "portable.txt"

# Set by the supervisor on its children. Children then treat EOF on stdin as
# "please shut down gracefully" (see lifeline.py) and /api/system/restart asks the
# supervisor instead of spawning scripts.
SUPERVISED_ENV = "INDIEPULSE_SUPERVISED"
RESTART_REQUEST_FILE = DATA / ".restart_request"


def is_supervised() -> bool:
	return os.getenv(SUPERVISED_ENV) == "1"
