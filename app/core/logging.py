from __future__ import annotations

import os
import sys
import time
from typing import Callable

from loguru import logger


def _redact_record(record) -> None:
	from app.core.redact import redact
	record["message"] = redact(record["message"])
	if record["exception"] is not None:
		# the traceback text is rendered from the exception value — scrub it too
		exc = record["exception"]
		try:
			if exc.value is not None and exc.value.args:
				exc.value.args = tuple(redact(a) if isinstance(a, str) else a for a in exc.value.args)
		except Exception:
			pass


def setup_logging() -> None:
	logger.remove()
	logger.configure(patcher=_redact_record)
	# Console
	logger.add(sys.stdout, level=os.getenv("LOG_LEVEL", "INFO"),
		format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {message}")
	# File
	os.makedirs("logs", exist_ok=True)
	logger.add(
		"logs/app.log",
		rotation="10 MB",
		retention="10 days",
		level=os.getenv("LOG_LEVEL", "INFO"),
		format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<7} | {message}",
	)


def request_logger():
	from fastapi import Request
	from starlette.responses import Response

	async def middleware(request: Request, call_next: Callable[[Request], Response]):
		start = time.perf_counter()
		response: Response
		try:
			response = await call_next(request)
		except Exception as exc:
			logger.exception(f"{request.method} {request.url.path} -> 500 error: {exc}")
			raise
		if request.url.path == "/health":
			return response  # polled by the tray and every open tab
		elapsed_ms = (time.perf_counter() - start) * 1000
		logger.info(f"{request.method} {request.url.path} -> {response.status_code} in {elapsed_ms:.1f}ms")
		return response

	return middleware


