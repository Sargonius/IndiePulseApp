"""Run the web server (what the tray supervisor starts): `python -m app.serve`.

Same as `uvicorn app.main:app`, plus a graceful stop when the supervisor goes away
and the `.autoreload` toggle from the header.
"""
import os

from dotenv import load_dotenv

load_dotenv()

import uvicorn
from uvicorn.supervisors import ChangeReload

from app.core import config
from app.desktop.lifeline import on_supervisor_gone


def main() -> None:
	reload = os.path.exists(".autoreload")
	cfg = uvicorn.Config(
		"app.main:app", host=config.APP_HOST, port=config.APP_PORT,
		reload=reload, reload_dirs=["app", "app/templates", "app/static"] if reload else None,
		access_log=False,  # app.core.logging logs requests itself (minus /health polling)
	)
	server = uvicorn.Server(cfg)
	if cfg.should_reload:
		# what uvicorn.run() does, but keeping the reloader so we can stop it
		reloader = ChangeReload(cfg, target=server.run, sockets=[cfg.bind_socket()])
		on_supervisor_gone(reloader.should_exit.set)
		reloader.run()
	else:
		on_supervisor_gone(lambda: setattr(server, "should_exit", True))
		server.run()


if __name__ == "__main__":
	main()
