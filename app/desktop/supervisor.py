"""Starts and watches the two processes the app is made of: the web server
(`app.serve`) and the Huey worker (`app.worker`).

- children run windowless, their output goes to logs/server.log and logs/worker.log;
- they live in a kill-on-close job, so they can't outlive the tray;
- a crashed child is restarted with backoff; 5 crashes in 5 minutes = give up and say so;
- the server is also pinged on /health; hung for 2 minutes = restarted;
- leftovers of a previous run (old start.bat windows, a crashed tray) are killed at start.
"""
from __future__ import annotations

import os
import queue
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from typing import Callable, Optional

import psutil
from loguru import logger

from app.desktop import DATA, RESTART_REQUEST_FILE, ROOT, SUPERVISED_ENV, deps
from app.desktop.winapi import CREATE_NO_WINDOW, KillOnCloseJob

LOGS = DATA / "logs"
_BACKOFF = (1, 3, 10, 30)
_CRASH_WINDOW = 300
_MAX_CRASHES = 5
_HUNG_AFTER = 120      # s without a /health answer while running
_START_TIMEOUT = 90    # s for the server to answer /health after start
_LEFTOVER_MARKERS = ("app.serve", "app.main:app", "app.worker")


def _kill_tree(pid: int) -> None:
	try:
		root = psutil.Process(pid)
		procs = root.children(recursive=True) + [root]
	except psutil.NoSuchProcess:
		return
	for p in procs:
		try:
			p.kill()
		except psutil.Error:
			pass
	psutil.wait_procs(procs, timeout=5)


def _same_dir(path: Optional[str]) -> bool:
	try:
		return bool(path) and Path(path).resolve() == ROOT
	except OSError:
		return False


class Child:
	def __init__(self, name: str, module: str, title: str, stop_timeout: float):
		self.name, self.module, self.title = name, module, title
		self.stop_timeout = stop_timeout
		self.proc: Optional[subprocess.Popen] = None
		self.state = "stopped"  # starting | running | backoff | failed | stopping | stopped
		self.started_at = 0.0
		self.last_ok = 0.0
		self.next_start_at = 0.0
		self.crashes: list[float] = []

	@property
	def alive(self) -> bool:
		return self.proc is not None and self.proc.poll() is None

	def start(self, job: KillOnCloseJob, env: dict) -> None:
		LOGS.mkdir(exist_ok=True)
		log_path = LOGS / f"{self.name}.log"
		try:  # keep the previous run's output for post-mortems
			if log_path.exists():
				os.replace(log_path, LOGS / f"{self.name}.prev.log")
		except OSError:
			pass
		python = deps.venv_python()
		with open(log_path, "ab", buffering=0) as log:
			self.proc = subprocess.Popen(
				[str(python), "-m", self.module], cwd=ROOT, env=env,  # the child itself moves into DATA
				stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
				creationflags=CREATE_NO_WINDOW,
			)
		if not job.assign(self.proc):
			logger.warning(f"{self.name}: could not add pid {self.proc.pid} to the job object")
		self.state = "starting"
		self.started_at = time.monotonic()
		logger.info(f"{self.name} started, pid {self.proc.pid}")

	def request_stop(self) -> None:
		if self.alive:
			self.state = "stopping"
			try:
				self.proc.stdin.close()  # EOF = "shut down gracefully" (lifeline.py)
			except OSError:
				pass

	def wait_stopped(self) -> None:
		if self.proc is None:
			self.state = "stopped"
			return
		try:
			self.proc.wait(self.stop_timeout)
			logger.info(f"{self.name} stopped (code {self.proc.returncode})")
		except subprocess.TimeoutExpired:
			logger.warning(f"{self.name} did not stop in {self.stop_timeout:.0f}s, killing it")
			_kill_tree(self.proc.pid)
		self.proc = None
		self.state = "stopped"


class Supervisor:
	def __init__(self, host: str, port: int, *, on_change: Callable[[], None],
				on_ready: Callable[[], None], on_stopped: Callable[[], None],
				notify: Callable[[str], None]):
		self.port = port
		self.health_url = f"http://{host}:{port}/health"
		self.on_change, self.on_ready, self.on_stopped, self.notify = on_change, on_ready, on_stopped, notify
		self.server = Child("server", "app.serve", "Server", stop_timeout=15)
		# the worker finishes the task in progress (a publish, a render) before exiting
		self.worker = Child("worker", "app.worker", "Worker", stop_timeout=90)
		self.phase = "starting"  # starting | updating | running | restarting | stopping | stopped
		self.problem = ""        # blocking error shown in the tray (port taken, ...)
		self._job = KillOnCloseJob()
		self._cmd: "queue.Queue[str]" = queue.Queue()
		self._next_health = 0.0
		self._ready_fired = False
		self._last_signature = None
		self._env = {**os.environ, SUPERVISED_ENV: "1", "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}

	# ── API for the tray (any thread) ────────────────────────────────────────
	def start(self) -> None:
		threading.Thread(target=self._run, name="supervisor", daemon=True).start()

	def restart(self) -> None:
		self._cmd.put("restart")

	def quit(self) -> None:
		self._cmd.put("quit")

	@property
	def healthy(self) -> bool:
		return self.server.state == "running" and self.worker.state == "running"

	def status(self) -> str:
		"""ok | busy | error — drives the tray icon colour."""
		if self.problem or "failed" in (self.server.state, self.worker.state):
			return "error"
		if self.healthy and self.phase == "running":
			return "ok"
		return "busy"

	def child_line(self, c: Child) -> str:
		text = {
			"starting": "starting…",
			"running": "running",
			"backoff": f"crashed, restarting in {max(0, int(c.next_start_at - time.monotonic()))} s",
			"failed": f"won’t start (logs\\{c.name}.log)",
			"stopping": "stopping…" if c is self.server else "finishing the current task…",
			"stopped": "stopped",
		}.get(c.state, c.state)
		return f"{c.title}: {text}"

	def summary(self) -> str:
		if self.problem:
			return self.problem
		return {
			"updating": "updating dependencies…",
			"starting": "starting…",
			"restarting": "restarting…",
			"stopping": "stopping…",
			"stopped": "stopped",
		}.get(self.phase) or ("running" if self.healthy else
							 "starting…" if "starting" in (self.server.state, self.worker.state) else
							 "crashed, restarting…")

	# ── supervisor thread ────────────────────────────────────────────────────
	def _changed(self, force: bool = False) -> None:
		sig = (self.phase, self.problem, self.server.state, self.worker.state,
			   int(self.server.next_start_at), int(self.worker.next_start_at))
		if force or sig != self._last_signature:
			self._last_signature = sig
			try:
				self.on_change()
			except Exception:
				logger.exception("tray update failed")

	def _run(self) -> None:
		try:
			self._sync_requirements()
			self._start_all()
			self.phase = "running"
			self._changed()
			while True:
				try:
					cmd = self._cmd.get(timeout=1.0)
				except queue.Empty:
					cmd = None
				if cmd == "quit":
					break
				if cmd == "restart" or self._restart_requested():
					self._restart_all()
					continue
				try:
					self._tick()
				except Exception:
					logger.exception("supervisor tick failed")
				self._changed()
		except Exception:
			logger.exception("supervisor crashed")
		finally:
			self.phase = "stopping"
			self._changed()
			self._stop_all()
			self.phase = "stopped"
			self._changed()
			self.on_stopped()

	def _sync_requirements(self) -> None:
		if not deps.requirements_changed():
			return
		self.phase = "updating"
		self._changed()
		logger.info("requirements.txt changed, running pip install")
		if deps.install_requirements(LOGS / "pip.log"):
			logger.info("dependencies updated")
		else:
			logger.error("pip install failed, see logs/pip.log")
			self.notify("Couldn’t update dependencies (logs\\pip.log). Starting as is.")

	def _restart_requested(self) -> bool:
		if RESTART_REQUEST_FILE.exists():
			try:
				RESTART_REQUEST_FILE.unlink()
			except OSError:
				pass
			logger.info("restart requested from the web UI")
			return True
		return False

	def _start_all(self) -> None:
		self.phase = "starting"
		self._changed()
		self._kill_leftovers()
		self._start(self.worker)
		if self._port_free():
			self._start(self.server)

	def _stop_all(self) -> None:
		for c in (self.server, self.worker):
			c.request_stop()
		self._changed()
		for c in (self.server, self.worker):
			c.wait_stopped()

	def _restart_all(self) -> None:
		logger.info("restarting server and worker")
		self.phase = "restarting"
		self.problem = ""
		self._changed()
		self._stop_all()
		for c in (self.server, self.worker):
			c.crashes.clear()
		self._sync_requirements()  # new code may need new packages (Restart after an update), not just a new tray
		self._start_all()
		self.phase = "running"
		self._changed()

	def _start(self, c: Child) -> None:
		try:
			c.start(self._job, self._env)
			if c is self.server:
				self._next_health = time.monotonic() + 1
		except Exception as exc:
			logger.exception(f"could not start {c.name}")
			self._crashed(c, f"start failed: {exc}")

	def _crashed(self, c: Child, why: str) -> None:
		now = time.monotonic()
		c.proc = None
		c.crashes = [t for t in c.crashes if now - t < _CRASH_WINDOW] + [now]
		if len(c.crashes) >= _MAX_CRASHES:
			c.state = "failed"
			logger.error(f"{c.name}: {why}; gave up after {len(c.crashes)} crashes")
			self.notify(f"{c.title} keeps crashing — see logs\\{c.name}.log and restart from the menu.")
		else:
			delay = _BACKOFF[min(len(c.crashes), len(_BACKOFF)) - 1]
			c.state = "backoff"
			c.next_start_at = now + delay
			logger.warning(f"{c.name}: {why}; restarting in {delay}s")

	def _tick(self) -> None:
		now = time.monotonic()
		for c in (self.server, self.worker):
			if c.state in ("starting", "running") and not c.alive:
				self._crashed(c, f"exited with code {c.proc.returncode if c.proc else '?'}")
			elif c.state == "backoff" and now >= c.next_start_at:
				if c is self.server and not self._port_free():
					c.state = "failed"
					continue
				self._start(c)

		w = self.worker
		if w.state == "starting" and now - w.started_at > 3:
			w.state = "running"

		s = self.server
		if s.state in ("starting", "running") and now >= self._next_health:
			self._next_health = now + 2
			if self._ping():
				s.last_ok = now
				if s.state == "starting":
					s.state = "running"
					logger.info("server is up")
					if not self._ready_fired:
						self._ready_fired = True
						self.on_ready()
			elif s.state == "starting" and now - s.started_at > _START_TIMEOUT:
				_kill_tree(s.proc.pid)
				self._crashed(s, f"no answer on /health {_START_TIMEOUT}s after start")
			elif s.state == "running" and now - s.last_ok > _HUNG_AFTER:
				_kill_tree(s.proc.pid)
				self._crashed(s, f"no answer on /health for {_HUNG_AFTER}s")

	def _ping(self) -> bool:
		try:
			with urllib.request.urlopen(self.health_url, timeout=2) as r:
				return r.status == 200
		except Exception:
			return False

	# ── leftovers / port ─────────────────────────────────────────────────────
	def _kill_leftovers(self) -> None:
		"""Server/worker processes of this folder not owned by a live tray: old start.bat
		windows (cmd.exe + python), or children of a tray that was killed hard."""
		me = os.getpid()
		victims = []
		# filter by name first: ppid/cmdline for every process takes ~15 s on Windows
		for p in psutil.process_iter(["pid", "name"]):
			try:
				if p.pid == me or (p.info["name"] or "").lower() not in ("python.exe", "pythonw.exe", "cmd.exe"):
					continue
				cmd = " ".join(p.cmdline())
				if not any(m in cmd for m in _LEFTOVER_MARKERS) or not _same_dir(p.cwd()):
					continue
				parent_cmd = ""
				ppid = p.ppid()
				if ppid and ppid != me:
					try:
						parent_cmd = " ".join(psutil.Process(ppid).cmdline())
					except psutil.Error:
						pass
				if "app.desktop.tray" in parent_cmd or "IndiePulse.pyw" in parent_cmd:
					continue  # belongs to another live tray (same folder, different port)
				victims.append(p)
			except psutil.Error:
				continue
		for p in victims:
			logger.info(f"killing leftover pid {p.pid} ({p.info['name']})")
			_kill_tree(p.pid)

	def _port_free(self) -> bool:
		"""Free our port from a stale server of this folder; report anything else holding it."""
		owners = set()
		try:
			for conn in psutil.net_connections(kind="tcp"):
				if conn.laddr and conn.laddr.port == self.port and conn.status == psutil.CONN_LISTEN and conn.pid:
					owners.add(conn.pid)
		except psutil.Error:
			return True
		for pid in owners:
			try:
				p = psutil.Process(pid)
				if p.name().lower().startswith("python") and _same_dir(p.cwd()):
					logger.info(f"port {self.port} held by stale pid {pid}, killing it")
					_kill_tree(pid)
					continue
				name = p.name()
			except psutil.Error:
				name = "?"
			self.problem = f"port {self.port} is taken: {name} (pid {pid})"
			logger.error(self.problem)
			self.notify(f"Port {self.port} is taken by another program ({name}, pid {pid}). "
						f"Close it or change APP_PORT in .env and restart.")
			self.server.state = "failed"
			return False
		self.problem = ""
		return True
