"""Desktop shell: tray icon + supervisor that runs the web server and the Huey worker.

What users start is IndiePulse.exe (launcher/: the first run is its setup, Install or Portable; it prepares Python
and the venv, then runs `pythonw -m app.desktop.tray`). A checkout without the exe built starts with
`python -m app.desktop.launch` (or `python -m app.desktop.tray` when its .venv is in place).
"""
import os

# DRIVE: IndiePulse.portable.ini, the optional settings of the drive IndiePulse is carried on
# (app/core/drive_settings.py): the uv to use and uv's folders, the computers trusted with shortcuts. Without one,
# IndiePulse behaves as an installed program.
from app.core.paths import DATA, DRIVE, HOME, ROOT, RUNTIME, is_release_layout  # noqa: F401  (re-exported)
PORTABLE_MARKER = HOME / "portable.txt"

# Set by the supervisor on its children. Children then treat EOF on stdin as
# "please shut down gracefully" (see lifeline.py) and /api/system/restart asks the
# supervisor instead of spawning scripts.
SUPERVISED_ENV = "INDIEPULSE_SUPERVISED"
RESTART_REQUEST_FILE = DATA / ".restart_request"


def is_supervised() -> bool:
	return os.getenv(SUPERVISED_ENV) == "1"
