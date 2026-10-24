"""IndiePulse launcher — double-click this file (or the IndiePulse shortcut).

No console windows: it makes sure .venv exists with the required packages, then runs
the tray app (app/desktop/tray.py), which starts the server and the worker.
`--background` = don't open the browser (used by "Start at Windows sign-in").
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from app.desktop import deps  # noqa: E402  (stdlib only)


def _message(text: str) -> None:
	import ctypes
	ctypes.windll.user32.MessageBoxW(None, text, "IndiePulse", 0x40)


def _tray_importable() -> bool:
	return subprocess.call(
		[str(deps.venv_python()), "-c", "import pystray, psutil, PIL, loguru, dotenv, uvicorn, huey"],
		cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=deps.CREATE_NO_WINDOW,
	) == 0


def main() -> None:
	if not deps.venv_alive():  # the first run, or the first one on this computer
		_message("First IndiePulse launch on this computer: installing dependencies (a minute or two).\n"
				 "When it’s done, an icon appears in the tray and the browser opens.")
		if not deps.ensure_venv(ROOT / "logs" / "venv.log"):
			_message("Couldn’t create the Python environment. Details: logs\\venv.log")
			sys.exit(1)
	# The tray syncs requirements itself (with a status in the icon); here only make
	# sure it can start at all.
	if deps.requirements_changed() and not _tray_importable():
		if not deps.install_requirements(ROOT / "logs" / "pip.log"):
			_message("Couldn’t install dependencies. Details: logs\\pip.log")
			sys.exit(1)

	if Path(sys.prefix).resolve() == (ROOT / ".venv").resolve():
		from app.desktop.tray import main as tray_main
		tray_main(sys.argv[1:])
	else:  # started by the system Python (pyw.exe): hand over to the venv
		subprocess.Popen([str(deps.venv_python(windowed=True)), "-m", "app.desktop.tray", *sys.argv[1:]],
						 cwd=ROOT, creationflags=deps.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)


if __name__ == "__main__":
	main()
