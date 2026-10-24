"""Desktop shell: supervisor protocol pieces that don't need a tray or real children."""
import os
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient

import app.desktop as desktop
from app.desktop import deps


@pytest.fixture
def client():
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def test_health_reports_boot_id(client):
    j = client.get("/health").json()
    assert j["status"] == "ok" and j["boot"]


def test_restart_needs_supervisor(client, monkeypatch):
    monkeypatch.delenv(desktop.SUPERVISED_ENV, raising=False)
    assert client.post("/api/system/restart").status_code == 409


def test_restart_asks_supervisor(client, monkeypatch, tmp_path):
    flag = tmp_path / ".restart_request"
    monkeypatch.setenv(desktop.SUPERVISED_ENV, "1")
    monkeypatch.setattr(desktop, "RESTART_REQUEST_FILE", flag)
    r = client.post("/api/system/restart")
    assert r.status_code == 200 and flag.exists()


def test_lifeline_stdin_eof_stops_child():
    """Closing the child's stdin is the graceful-stop request."""
    code = ("import time\n"
            "from app.desktop.lifeline import on_supervisor_gone\n"
            "done = []\n"
            "on_supervisor_gone(lambda: done.append(1))\n"
            "t = time.time()\n"
            "while not done and time.time() - t < 10: time.sleep(0.05)\n"
            "print('stopped' if done else 'timeout')\n")
    env = {**os.environ, desktop.SUPERVISED_ENV: "1"}
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         env=env, cwd=desktop.ROOT, text=True)
    time.sleep(0.5)
    assert p.poll() is None  # still waiting while the pipe is open
    p.stdin.close()
    assert p.stdout.read().strip() == "stopped"
    p.wait(5)


def test_lifeline_pipe_is_not_inherited_by_grandchildren():
    """ffmpeg started by the worker used to get the lifeline pipe as stdin and hang
    forever; now it must see an empty stdin (NUL)."""
    code = ("import subprocess, sys\n"
            "from app.desktop.lifeline import on_supervisor_gone\n"
            "on_supervisor_gone(lambda: None)\n"
            "try:\n"
            "    r = subprocess.run([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read()))'],\n"
            "                       stdout=subprocess.PIPE, text=True, timeout=10)\n"
            "    print(r.stdout.strip())\n"
            "except subprocess.TimeoutExpired:\n"
            "    print('hung')\n")
    env = {**os.environ, desktop.SUPERVISED_ENV: "1"}
    p = subprocess.Popen([sys.executable, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         env=env, cwd=desktop.ROOT, text=True)
    try:
        out = p.stdout.read()  # stdin stays open: the supervisor is "alive"
    finally:
        p.stdin.close()
        p.wait(5)
    assert out.strip() == "''"


def test_requirements_stamp(monkeypatch, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("fastapi\n")
    monkeypatch.setattr(deps, "REQUIREMENTS", req)
    monkeypatch.setattr(deps, "_STAMP", tmp_path / "stamp")
    assert deps.requirements_changed()
    deps._STAMP.write_text(deps._requirements_hash())
    assert not deps.requirements_changed()
    req.write_text("fastapi\npystray\n")
    assert deps.requirements_changed()


def test_crash_backoff_then_give_up():
    from app.desktop.supervisor import Supervisor, _MAX_CRASHES
    notes = []
    sup = Supervisor("127.0.0.1", 1, on_change=lambda: None, on_ready=lambda: None,
                     on_stopped=lambda: None, notify=notes.append)
    w = sup.worker
    sup._crashed(w, "boom")
    assert w.state == "backoff" and w.next_start_at > time.monotonic() and sup.status() == "busy"
    for _ in range(_MAX_CRASHES - 1):
        sup._crashed(w, "boom")
    assert w.state == "failed" and sup.status() == "error" and notes
