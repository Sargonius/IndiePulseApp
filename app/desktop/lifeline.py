"""Child side of the supervisor protocol.

The supervisor starts each child with stdin as a pipe and never writes to it. Closing
the pipe (supervisor asks to stop, or the supervisor itself died) makes stdin hit EOF,
and the child shuts down gracefully — the worker finishes its current task first.
Windows has no SIGTERM for windowless processes, so this is the portable way to ask.
"""
from __future__ import annotations

import sys
import threading
from typing import Callable

from app.desktop import is_supervised


def on_supervisor_gone(callback: Callable[[], None]) -> None:
	if not is_supervised() or sys.stdin is None:
		return

	def _wait() -> None:
		try:
			while sys.stdin.buffer.read(4096):
				pass
		except Exception:
			pass
		callback()

	threading.Thread(target=_wait, name="supervisor-lifeline", daemon=True).start()
