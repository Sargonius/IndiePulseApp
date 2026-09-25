"""IndiePulse tray app: `pythonw -m app.desktop.tray [--background]` (normally via IndiePulse.pyw).

One icon in the notification area instead of two console windows. Its colour is the
app state (green ok / amber starting or restarting / red broken), the menu opens the UI,
restarts, shows logs, and "Выход" stops the server and worker (the worker finishes the
task in progress first). Launching again while it runs just opens the browser.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser

from app.desktop import ROOT

os.chdir(ROOT)

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

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
			notify("Ярлыки IndiePulse созданы на рабочем столе и в меню Пуск.")
		except Exception:
			logger.exception("shortcut creation failed")
			notify("Не удалось создать ярлыки (logs\\launcher.log).")

	def quit_app() -> None:
		sup.quit()

	icon = pystray.Icon(
		"IndiePulse", tray_image("busy"), "IndiePulse — запуск…",
		menu=Menu(
			Item("Открыть IndiePulse", open_ui, default=True),
			Menu.SEPARATOR,
			Item(lambda _: sup.child_line(sup.server), None, enabled=False),
			Item(lambda _: sup.child_line(sup.worker), None, enabled=False),
			Menu.SEPARATOR,
			Item("Перезапустить", sup.restart, enabled=lambda _: sup.phase in ("running",)),
			Item("Открыть папку логов", open_logs),
			Item("Запускать при входе в Windows", toggle_autostart, checked=lambda _: winapi.autostart_enabled()),
			Item("Создать ярлыки (рабочий стол, Пуск)", make_shortcuts),
			Menu.SEPARATOR,
			Item("Выход", quit_app),
		),
	)

	def setup(ic: pystray.Icon) -> None:
		ic.visible = True
		sup.start()
		if not _SHORTCUTS_MARKER.exists():
			make_shortcuts()  # first run: behave like an installed program

	logger.info(f"IndiePulse tray starting ({url}, background={background})")
	icon.run(setup=setup)
	logger.info("IndiePulse tray exited")


def main(argv: list[str] | None = None) -> None:
	parser = argparse.ArgumentParser(prog="IndiePulse")
	parser.add_argument("--background", action="store_true", help="don't open the browser (autostart)")
	args = parser.parse_args(argv)

	(ROOT / "logs").mkdir(exist_ok=True)
	logger.remove()  # pythonw has no stdout
	logger.add(ROOT / "logs" / "launcher.log", rotation="2 MB", retention=3, encoding="utf-8",
			   format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {message}")
	try:
		run(args.background)
	except Exception as exc:
		logger.exception("tray crashed")
		winapi.message_box(f"IndiePulse не смог запуститься:\n{exc}\n\nПодробности: logs\\launcher.log")
		sys.exit(1)


if __name__ == "__main__":
	main()
