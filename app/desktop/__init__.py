"""Desktop shell: tray icon + supervisor that runs the web server and the Huey worker.

Entry point for users is `IndiePulse.pyw` in the project root (double-click / shortcut);
it bootstraps the venv and then runs `python -m app.desktop.tray`.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# Set by the supervisor on its children. Children then treat EOF on stdin as
# "please shut down gracefully" (see lifeline.py) and /api/system/restart asks the
# supervisor instead of spawning scripts.
SUPERVISED_ENV = "INDIEPULSE_SUPERVISED"
RESTART_REQUEST_FILE = ROOT / ".restart_request"


def is_supervised() -> bool:
	return os.getenv(SUPERVISED_ENV) == "1"
