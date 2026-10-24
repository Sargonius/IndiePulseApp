"""Child side of the supervisor protocol.

The supervisor starts each child with stdin as a pipe and never writes to it. Closing
the pipe (supervisor asks to stop, or the supervisor itself died) makes stdin hit EOF,
and the child shuts down gracefully — the worker finishes its current task first.
Windows has no SIGTERM for windowless processes, so this is the portable way to ask.
"""
from __future__ import annotations

import os
import sys
import threading
from typing import Callable

from app.desktop import is_supervised

_kept_open: list = []


def _keep_pipe_from_children() -> None:
	"""Subprocesses inherit the process std handles. With the lifeline pipe as its stdin,
	ffmpeg's keyboard check queues behind our pending read (Windows serializes synchronous
	I/O on one pipe) and hangs forever — it froze a Reddit publish on 2026-09-30 with the
	whole queue behind it. Children get NUL instead; sys.stdin keeps its own handle."""
	if os.name != "nt":
		return
	import ctypes
	import msvcrt
	nul = open(os.devnull, "rb")
	_kept_open.append(nul)  # the handle must outlive every child we start
	STD_INPUT_HANDLE = -10
	ctypes.windll.kernel32.SetStdHandle(STD_INPUT_HANDLE, msvcrt.get_osfhandle(nul.fileno()))


def on_supervisor_gone(callback: Callable[[], None]) -> None:
	if not is_supervised() or sys.stdin is None:
		return
	try:
		_keep_pipe_from_children()
	except Exception:
		pass  # worst case children see the pipe, as before

	def _wait() -> None:
		try:
			while sys.stdin.buffer.read(4096):
				pass
		except Exception:
			pass
		callback()

	threading.Thread(target=_wait, name="supervisor-lifeline", daemon=True).start()
