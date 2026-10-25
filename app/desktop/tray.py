"""IndiePulse tray app: `pythonw -m app.desktop.tray [--background]`, started by IndiePulse.exe (launcher/).

One icon in the notification area instead of two console windows. Its colour is the
app state (green ok / amber starting or restarting / red broken), the menu opens the UI —
IndiePulse's own window (app/desktop/window.py) — restarts, shows logs, checks for and installs
updates (app/desktop/updater.py), and "Quit" stops the server and worker (the worker finishes
the task in progress first) and closes the window. IndiePulse.exe started again while this runs
asks it to bring up the window (winapi.InstanceEvents); its uninstaller asks it to quit.
`--background` (autostart, after an update): no window.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

from app.core.paths import enter_data_dir
from app.desktop import DATA, DRIVE, ROOT, is_release_layout

enter_data_dir()

from dotenv import load_dotenv

load_dotenv(DATA / ".env")

from loguru import logger

from app import __version__
from app.core import config
from app.desktop import deps, window, winapi
from app.desktop.icon import ensure_ico, tray_image

ICO_PATH = ROOT / "app" / "static" / "indiepulse.ico"
# The shortcuts the tray made itself, and for which command (winapi.refresh_shortcuts); in the venv: a new computer's
# new venv makes them again
_SHORTCUTS_MARKER = deps.VENV / "indiepulse-shortcuts"


def _first_run_shortcuts(trusted: bool) -> bool:
	"""The first run on a computer makes shortcuts, like an installed program: on a computer of the drive settings'
	trusted_pcs, or for a checkout. A release's own Install made them already, and its Portable promised to leave
	nothing."""
	return not _SHORTCUTS_MARKER.exists() and trusted and (DRIVE.trusted_pcs is not None or not is_release_layout())


def _local_host() -> str:
	return config.APP_HOST if config.APP_HOST not in ("0.0.0.0", "::", "") else "127.0.0.1"


def _refresh_traces(trusted: bool) -> None:
	"""What an older IndiePulse left pointing at the old way of starting it (IndiePulse.pyw), and the version Settings
	→ Apps shows: brought up to date. Nothing new is created here."""
	try:
		if winapi.refresh_autostart():
			logger.info("autostart entry now starts IndiePulse.exe")
		if trusted and winapi.refresh_shortcuts(_SHORTCUTS_MARKER, ensure_ico(ICO_PATH)):
			logger.info("shortcuts made again for the way IndiePulse starts now")
		if winapi.refresh_uninstall_entry(__version__):
			logger.info(f"uninstall entry says {__version__} now")
	except Exception:
		logger.exception("refreshing shortcuts / autostart / uninstall entry failed")


def run(background: bool) -> None:
	url = f"http://{_local_host()}:{config.APP_PORT}/"
	if not winapi.acquire_single_instance(config.APP_PORT):
		logger.info("already running")
		if not background:
			window.open_window(url)
		winapi.InstanceEvents.signal_ready()  # an exe waiting for this start: it's up (the other tray's)
		return
	events = winapi.InstanceEvents()  # from here on IndiePulse.exe sees this tray running

	import pystray
	from pystray import Menu, MenuItem as Item

	from app.desktop.supervisor import LOGS, Supervisor

	icon: pystray.Icon
	want_window = [not background]  # open the UI once the server answers
	# A club computer the drive only visits, or a portable copy, gets no shortcuts and no autostart entry
	trusted = winapi.trusted_pc()

	def refresh() -> None:
		icon.icon = tray_image(sup.status())
		icon.title = f"IndiePulse — {sup.summary()}"[:127]
		icon.update_menu()

	def on_ready() -> None:
		if want_window[0]:
			want_window[0] = False
			window.open_window(url)
		winapi.InstanceEvents.signal_ready()  # IndiePulse.exe's setup window closes as this one opens

	def notify(text: str) -> None:
		try:
			icon.notify(text, "IndiePulse")
		except Exception:
			logger.exception("notify failed")

	sup = Supervisor(_local_host(), config.APP_PORT, on_change=refresh, on_ready=on_ready,
					 on_stopped=lambda: icon.stop(), notify=notify)
	# checks GitHub 30 s after start and every 6 h; installs by handing over to apply_update.py and quitting
	from app.desktop.updater import TrayUpdater
	updater = TrayUpdater(sup, notify=notify, refresh=lambda: icon.update_menu(), base_url=url)

	def open_ui() -> None:
		if sup.server.state == "running":
			window.open_window(url)
		elif not window.bring_to_front():
			want_window[0] = True  # will open as soon as it's up

	def open_logs() -> None:
		LOGS.mkdir(exist_ok=True)
		os.startfile(LOGS)

	def toggle_autostart() -> None:
		winapi.set_autostart(not winapi.autostart_enabled())
		icon.update_menu()

	def make_shortcuts() -> None:
		"""The menu's: this copy's shortcuts, whatever the ones there started."""
		try:
			winapi.create_shortcuts(ensure_ico(ICO_PATH))
			_SHORTCUTS_MARKER.write_text(winapi.shortcuts_stamp(), encoding="utf-8")
			notify("IndiePulse shortcuts created on the desktop and in the Start menu.")
		except Exception:
			logger.exception("shortcut creation failed")
			notify("Couldn’t create shortcuts (logs\\launcher.log).")

	def first_run_shortcuts() -> None:
		"""The first run's: not over shortcuts that start another IndiePulse that is there (a second copy on the drive,
		a test one) — those stay, and this copy's own are a click in the menu."""
		try:
			free, taken = winapi.free_shortcut_paths(winapi.shortcut_paths())
			if free:
				winapi.create_shortcuts(ensure_ico(ICO_PATH), free)
			_SHORTCUTS_MARKER.write_text(winapi.shortcuts_stamp(), encoding="utf-8")
			for path, target in taken.items():
				logger.info(f"shortcut left as it is, it starts another IndiePulse: {path} → {target}")
			if taken:
				notify("Shortcuts of another IndiePulse left as they are. This one’s own: “Create shortcuts” in this menu.")
			elif free:
				notify("IndiePulse shortcuts created on the desktop and in the Start menu.")
		except Exception:
			logger.exception("shortcut creation failed")

	def quit_app() -> None:
		window.close_windows()
		sup.quit()

	def youtube_label() -> str:
		from app.services.settings import load_settings
		from app.services.youtube_setup import tray_menu_label
		try:
			return tray_menu_label(load_settings())
		except Exception:
			return ""

	def reconnect_youtube() -> None:
		if sup.server.state == "running":
			window.open_window(url + "api/accounts/connect/youtube/go", new=True)
		else:
			notify("The server is still starting — try again in a minute.")

	def youtube_watch() -> None:
		"""Weekly-reconnect reminder (apps in Google's Testing status get 7-day YouTube
		tokens): one balloon on the last day, one when YouTube stops; fresh menu label.
		Also one balloon each time the prepaid X API credits run out."""
		from app.services import x_billing
		from app.services.settings import load_settings
		from app.services.youtube_setup import tray_reminder
		shown: set = set()
		while True:
			try:
				cfg = load_settings()
				for reminder in (tray_reminder(cfg), x_billing.tray_reminder(cfg)):
					if reminder and reminder[0] not in shown:
						shown.add(reminder[0])
						notify(reminder[1])
				icon.update_menu()
			except Exception:
				logger.exception("tray reminder failed")
			time.sleep(600)

	icon = pystray.Icon(
		"IndiePulse", tray_image("busy"), "IndiePulse — starting…",
		menu=Menu(
			Item("Open IndiePulse", open_ui, default=True),
			Item(lambda _: youtube_label() or "YouTube", reconnect_youtube, visible=lambda _: bool(youtube_label())),
			Item(lambda _: updater.install_label(), updater.install, visible=lambda _: updater.can_install()),
			Menu.SEPARATOR,
			Item(lambda _: sup.child_line(sup.server), None, enabled=False),
			Item(lambda _: sup.child_line(sup.worker), None, enabled=False),
			Menu.SEPARATOR,
			Item("Restart", sup.restart, enabled=lambda _: sup.phase in ("running",)),
			Item("Open logs folder", open_logs),
			Item("Check for updates", updater.check_now),
			Item("Start at Windows sign-in", toggle_autostart, checked=lambda _: winapi.autostart_enabled(),
				 visible=lambda _: trusted or winapi.autostart_enabled()),  # left on: still can be turned off
			Item("Create shortcuts (desktop, Start menu)", make_shortcuts, visible=lambda _: trusted),
			Menu.SEPARATOR,
			Item("Quit", quit_app),
		),
	)

	def setup(ic: pystray.Icon) -> None:
		ic.visible = True
		sup.start()
		events.listen(on_show=open_ui, on_quit=quit_app)
		threading.Thread(target=youtube_watch, name="youtube-reminder", daemon=True).start()
		updater.start()
		_refresh_traces(trusted)
		if _first_run_shortcuts(trusted):
			first_run_shortcuts()

	logger.info(f"IndiePulse tray starting ({url}, background={background})")
	icon.run(setup=setup)
	logger.info("IndiePulse tray exited")


def main(argv: list[str] | None = None) -> None:
	parser = argparse.ArgumentParser(prog="IndiePulse")
	parser.add_argument("--background", action="store_true", help="don't open the window (autostart, an update)")
	args = parser.parse_args(argv)

	(DATA / "logs").mkdir(exist_ok=True)
	logger.remove()  # pythonw has no stdout
	logger.add(DATA / "logs" / "launcher.log", rotation="2 MB", retention=3, encoding="utf-8",
			   format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {message}")
	if DRIVE.file:
		logger.info(f"drive settings: {DRIVE.file}")
	for problem in DRIVE.problems:
		logger.warning(f"{DRIVE.file}: {problem}")
	try:
		run(args.background)
	except Exception as exc:
		logger.exception("tray crashed")
		winapi.message_box(f"IndiePulse couldn’t start:\n{exc}\n\nDetails: logs\\launcher.log")
		sys.exit(1)


if __name__ == "__main__":
	main()
