"""IndiePulse tray app: `pythonw -m app.desktop.tray [--background]` (normally via IndiePulse.pyw).

One icon in the notification area instead of two console windows. Its colour is the
app state (green ok / amber starting or restarting / red broken), the menu opens the UI,
restarts, shows logs, checks for and installs updates (app/desktop/updater.py), and "Quit"
stops the server and worker (the worker finishes the task in progress first). Launching
again while it runs just opens the browser.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import webbrowser

from app.core.paths import enter_data_dir
from app.desktop import DATA, ROOT

enter_data_dir()

from dotenv import load_dotenv

load_dotenv(DATA / ".env")

from loguru import logger

from app.core import config
from app.desktop import winapi
from app.desktop.icon import ensure_ico, tray_image

ICO_PATH = ROOT / "app" / "static" / "indiepulse.ico"
_SHORTCUTS_MARKER = ROOT / ".venv" / "indiepulse-shortcuts"


def _local_host() -> str:
	return config.APP_HOST if config.APP_HOST not in ("0.0.0.0", "::", "") else "127.0.0.1"


def run(background: bool) -> None:
	url = f"http://{_local_host()}:{config.APP_PORT}/"
	if not winapi.acquire_single_instance(config.APP_PORT):
		logger.info("already running")
		if not background:
			webbrowser.open(url)
		return

	import pystray
	from pystray import Menu, MenuItem as Item

	from app.desktop.supervisor import LOGS, Supervisor

	icon: pystray.Icon
	want_browser = [not background]  # open the UI once the server answers
	# A club computer the drive only visits gets no shortcuts and no autostart entry: nothing left behind
	trusted = winapi.trusted_pc()

	def refresh() -> None:
		icon.icon = tray_image(sup.status())
		icon.title = f"IndiePulse — {sup.summary()}"[:127]
		icon.update_menu()

	def on_ready() -> None:
		if want_browser[0]:
			want_browser[0] = False
			webbrowser.open(url)

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
			webbrowser.open(url)
		else:
			want_browser[0] = True  # will open as soon as it's up

	def open_logs() -> None:
		LOGS.mkdir(exist_ok=True)
		os.startfile(LOGS)

	def toggle_autostart() -> None:
		winapi.set_autostart(not winapi.autostart_enabled())
		icon.update_menu()

	def make_shortcuts() -> None:
		try:
			winapi.create_shortcuts(ensure_ico(ICO_PATH))
			_SHORTCUTS_MARKER.touch()
			notify("IndiePulse shortcuts created on the desktop and in the Start menu.")
		except Exception:
			logger.exception("shortcut creation failed")
			notify("Couldn’t create shortcuts (logs\\launcher.log).")

	def quit_app() -> None:
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
			webbrowser.open(url + "api/accounts/connect/youtube/go")
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
		threading.Thread(target=youtube_watch, name="youtube-reminder", daemon=True).start()
		updater.start()
		if not _SHORTCUTS_MARKER.exists() and trusted:
			make_shortcuts()  # first run: behave like an installed program — not on a club computer the drive visits

	logger.info(f"IndiePulse tray starting ({url}, background={background})")
	icon.run(setup=setup)
	logger.info("IndiePulse tray exited")


def main(argv: list[str] | None = None) -> None:
	parser = argparse.ArgumentParser(prog="IndiePulse")
	parser.add_argument("--background", action="store_true", help="don't open the browser (autostart)")
	args = parser.parse_args(argv)

	(DATA / "logs").mkdir(exist_ok=True)
	logger.remove()  # pythonw has no stdout
	logger.add(DATA / "logs" / "launcher.log", rotation="2 MB", retention=3, encoding="utf-8",
			   format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {message}")
	try:
		run(args.background)
	except Exception as exc:
		logger.exception("tray crashed")
		winapi.message_box(f"IndiePulse couldn’t start:\n{exc}\n\nDetails: logs\\launcher.log")
		sys.exit(1)


if __name__ == "__main__":
	main()
