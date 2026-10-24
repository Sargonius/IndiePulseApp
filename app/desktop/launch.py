"""Starting IndiePulse without IndiePulse.exe: a checkout where the exe isn't built (tools/build_launcher.ps1).

	py -3.12 -m app.desktop.launch [--background]          # from the checkout's folder
	pythonw.exe <checkout>\\app\\desktop\\launch.py [--background]

It makes sure the .venv exists with the required packages (app/desktop/deps.py; no console windows, a message box
on the first run) and runs the tray (app/desktop/tray.py) in it, which starts the server and the worker. The
shortcuts and the autostart entry of a checkout without the exe point here. What used to be IndiePulse.pyw.
"""
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a file: the checkout, not this folder, is where `app` is imported from
	_here = str(Path(__file__).resolve().parent)
	sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != Path(_here)]
	sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.desktop import DATA, ROOT, deps  # noqa: E402  (stdlib only)


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
				 "When it’s done, an icon appears in the tray and IndiePulse opens.")
		if not deps.ensure_venv(DATA / "logs" / "venv.log"):
			_message("Couldn’t create the Python environment. Details: logs\\venv.log")
			sys.exit(1)
	# The tray syncs requirements itself (with a status in the icon); here only make sure it can start at all.
	if deps.requirements_changed() and not _tray_importable():
		if not deps.install_requirements(DATA / "logs" / "pip.log"):
			_message("Couldn’t install dependencies. Details: logs\\pip.log")
			sys.exit(1)

	if Path(sys.prefix).resolve() == deps.VENV.resolve():
		from app.desktop.tray import main as tray_main
		tray_main(sys.argv[1:])
	else:  # started by another Python (py -3.12): hand over to the venv's
		subprocess.Popen([str(deps.venv_python(windowed=True)), "-m", "app.desktop.tray", *sys.argv[1:]],
						 cwd=ROOT, creationflags=deps.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS)


if __name__ == "__main__":
	main()
