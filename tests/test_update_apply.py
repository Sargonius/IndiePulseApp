"""The installer (app/desktop/apply_update.py): the swap and its undo, the rollback, and the whole run with the
processes faked — on temp folders only. A release is IndiePulse.exe, README.md, LICENSE and lib\\ (the code); the
folder it is installed in also has runtime\\ (uv, Python, the venv) and, portable, data\\ and portable.txt."""
import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from app.desktop import apply_update as au


def _tree(base: Path, files: dict) -> Path:
    for rel, text in files.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text, encoding="utf-8")
    return base


def _release(version):
    return {"lib/app/__init__.py": f'__version__ = "{version}"\n', "lib/app/main.py": f"# {version}\n",
            "IndiePulse.exe": f"MZ {version}\n", "lib/requirements.txt": "",
            "lib/requirements.lock": f"# {version}\n", "README.md": f"# {version}\n"}


@pytest.fixture
def install(tmp_path):
    """A portable 2.0.0 with its runtime and data, and 2.1.0 unpacked in the updates folder."""
    root = _tree(tmp_path / "IndiePulse", {
        **_release("2.0.0"),
        "lib/app/services/removed_in_2_1.py": "# gone in 2.1.0\n",
        "LICENSE": "old license\n",  # 2.1.0 has none: stays
        "runtime/venv/pyvenv.cfg": "home = C:\\IndiePulse\\runtime\\python\n",
        "runtime/python/cpython-3.12/python.exe": "MZ python",
        "runtime/uv/uv.exe": "MZ uv",
        ".env": "APP_PORT=8000\n",
        "portable.txt": "",
        "data/indiepulse.db": "the user's posts",
        "data/.settings.json": '{"tg_bot_token": "secret"}',
        "data/browser/Default/Cookies": "the app window's profile",
    })
    work = tmp_path / "IndiePulse" / "data" / "updates"
    staged = _tree(work / "staging" / "IndiePulse", {**_release("2.1.0"), "lib/app/services/new_in_2_1.py": "# new\n",
                                                     "CHANGELOG.md": "new file\n"})
    return root, work, staged


def _kept(root):
    return {p: (root / p).read_text(encoding="utf-8") for p in (
        "runtime/venv/pyvenv.cfg", "runtime/python/cpython-3.12/python.exe", "runtime/uv/uv.exe", ".env", "portable.txt",
        "data/indiepulse.db", "data/.settings.json", "data/browser/Default/Cookies")}


def test_swap_replaces_the_code_and_nothing_else(install):
    root, work, staged = install
    before = _kept(root)
    record = au.swap(root, staged, work / "previous")
    assert au.version_of(root) == "2.1.0"
    assert (root / "lib/app/services/new_in_2_1.py").exists()
    assert not (root / "lib/app/services/removed_in_2_1.py").exists()  # lib\\ is replaced whole
    assert (root / "IndiePulse.exe").read_text() == "MZ 2.1.0\n"          # the exe too
    assert (root / "LICENSE").read_text() == "old license\n"        # not in the release: left alone
    assert (root / "CHANGELOG.md").exists()
    assert _kept(root) == before                                    # runtime, settings, data: untouched
    assert au.version_of(work / "previous") == "2.0.0"
    assert (work / "previous/lib/app/services/removed_in_2_1.py").exists()
    assert record["version"] == "2.0.0" and "CHANGELOG.md" in record["added"]
    assert {"lib", "IndiePulse.exe", "README.md"} <= set(record["replaced"])
    assert json.loads((work / "previous/previous.json").read_text()) == record
    assert not any(staged.iterdir())  # moved, not copied


def test_restore_puts_the_old_code_back(install):
    root, work, staged = install
    before = _kept(root)
    au.swap(root, staged, work / "previous")
    au.restore(root, work / "previous", work / "failed")
    assert au.version_of(root) == "2.0.0"
    assert (root / "lib/app/services/removed_in_2_1.py").exists() and not (root / "CHANGELOG.md").exists()
    assert (root / "IndiePulse.exe").read_text() == "MZ 2.0.0\n" and (root / "LICENSE").exists()
    assert _kept(root) == before
    assert au.version_of(work / "failed") == "2.1.0"  # the new code, aside for a look
    assert not (work / "previous/previous.json").exists()  # nothing left to roll back to


def test_a_failed_move_undoes_the_whole_swap(install, monkeypatch):
    root, work, staged = install
    real_move, calls = au._move, []

    def flaky(src, dst, attempts=40):
        calls.append(src)
        if len(calls) == 6:  # in the middle of the release's names
            raise PermissionError(13, "in use", str(src))
        real_move(src, dst, attempts)
    monkeypatch.setattr(au, "_move", flaky)
    with pytest.raises(au.SwapError, match="nothing was changed"):
        au.swap(root, staged, work / "previous")
    monkeypatch.setattr(au, "_move", real_move)
    assert au.version_of(root) == "2.0.0" and (root / "IndiePulse.exe").read_text() == "MZ 2.0.0\n"
    assert (root / "lib/app/services/removed_in_2_1.py").exists() and not (root / "CHANGELOG.md").exists()
    assert au.version_of(staged) == "2.1.0" and (staged / "IndiePulse.exe").read_text() == "MZ 2.1.0\n"


def test_a_locked_file_is_retried(tmp_path, monkeypatch):
    import os
    (tmp_path / "a").write_text("x")
    real_rename, tries = os.rename, []

    def locked_twice(src, dst):
        tries.append(1)
        if len(tries) <= 2:
            raise PermissionError(32, "being used by another process")
        real_rename(src, dst)
    monkeypatch.setattr(au.os, "rename", locked_twice)
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    au._move(tmp_path / "a", tmp_path / "b")
    assert len(tries) == 3 and (tmp_path / "b").read_text() == "x"


@pytest.mark.parametrize("bad", ["runtime/venv/pyvenv.cfg", ".venv/pyvenv.cfg", ".git/HEAD", "data/x", ".env",
                                 "portable.txt", "queue.db", "logs/x.log", "media/a.png", ".settings.json",
                                 "browser/Default/Cookies"])
def test_a_release_never_replaces_the_environment_or_data(install, bad):
    root, work, staged = install
    _tree(staged, {bad: "from the zip"})
    with pytest.raises(au.SwapError, match="never replaces"):
        au.swap(root, staged, work / "previous")
    assert au.version_of(root) == "2.0.0"


@pytest.mark.parametrize("missing", ["lib/requirements.lock", "IndiePulse.exe"])
def test_a_release_missing_files_is_refused(install, missing):
    root, work, staged = install
    (staged / missing).unlink()
    with pytest.raises(au.SwapError, match=missing):
        au.swap(root, staged, work / "previous")


def test_find_release_root(tmp_path):
    _tree(tmp_path / "s1" / "IndiePulse", _release("1.0.0"))
    assert au.find_release_root(tmp_path / "s1") == tmp_path / "s1" / "IndiePulse"
    _tree(tmp_path / "s2", _release("1.0.0"))
    assert au.find_release_root(tmp_path / "s2") == tmp_path / "s2"
    (tmp_path / "s3" / "a").mkdir(parents=True)
    with pytest.raises(au.SwapError):
        au.find_release_root(tmp_path / "s3")


# ── the whole run, processes faked ───────────────────────────────────────────
class FakeProc:
    def __init__(self, pid):
        self.pid, self.returncode = pid, None

    def poll(self):
        return self.returncode


@pytest.fixture
def run_env(install, monkeypatch):
    root, work, staged = install
    monkeypatch.setattr(au, "hold_updating_mutex", lambda home: None)
    (work / "IndiePulse-2.1.0.zip").write_bytes(b"zip")
    (work / "ready.json").write_text("{}")
    log = {"trays": [], "killed": [], "synced": [], "health": (True, "")}

    def start_tray(r):
        log["trays"].append(au.version_of(r))
        return FakeProc(1000 + len(log["trays"]))

    def sync(r, log_path):
        log["synced"].append(au.version_of(r))
        return log.get("sync_ok", True)
    monkeypatch.setattr(au, "wait_for_exit", lambda pid, timeout: True)
    monkeypatch.setattr(au, "start_tray", start_tray)
    monkeypatch.setattr(au, "sync_requirements", sync)
    monkeypatch.setattr(au, "kill_tree", lambda pid: log["killed"].append(pid))
    monkeypatch.setattr(au, "wait_healthy", lambda url, version, proc: log["health"])
    monkeypatch.setattr(au.time, "sleep", lambda s: None)
    args = argparse.Namespace(root=str(root), data=str(root / "data"), work=str(work), staged=str(staged),
                              version="2.1.0", from_version="2.0.0", pid=4242, url="http://127.0.0.1:8000/")
    return root, work, args, log


def test_run_installs_and_starts_the_new_version(run_env):
    root, work, args, log = run_env
    assert au.run(args) == 0
    assert au.version_of(root) == "2.1.0"
    assert log["synced"] == ["2.1.0"] and log["trays"] == ["2.1.0"] and log["killed"] == []
    p = au.read_json(work / "progress.json")
    assert p["phase"] == "installed" and p["version"] == "2.1.0" and p["from_version"] == "2.0.0"
    assert not (work / "staging").exists() and not (work / "ready.json").exists()
    assert not list(work.glob("*.zip"))
    assert au.version_of(work / "previous") == "2.0.0"  # kept until the next update
    assert "2.1.0 is up" in (root / "data/logs/update.log").read_text(encoding="utf-8")


def test_run_rolls_back_when_the_new_server_doesnt_answer(run_env):
    root, work, args, log = run_env
    log["health"] = (False, "its server didn't answer in 240 s")
    assert au.run(args) == 1
    assert au.version_of(root) == "2.0.0"
    assert (root / "lib/app/services/removed_in_2_1.py").exists()
    assert log["trays"] == ["2.1.0", "2.0.0"]   # the new one, killed, then the old one again
    assert log["killed"] == [1001]
    assert log["synced"] == ["2.1.0", "2.0.0"]  # the old requirements back too
    p = au.read_json(work / "progress.json")
    assert p["phase"] == "rolled_back" and "didn't answer" in p["text"] and "2.0.0 is back" in p["text"]
    assert au.version_of(work / "failed") == "2.1.0"
    assert not (work / "staging").exists() and not list(work.glob("*.zip"))


def test_run_rolls_back_when_the_packages_dont_install(run_env):
    root, work, args, log = run_env
    log["sync_ok"] = False
    assert au.run(args) == 1
    assert au.version_of(root) == "2.0.0" and log["trays"] == ["2.0.0"]
    assert "packages didn't install" in au.read_json(work / "progress.json")["text"]


def test_run_keeps_the_old_version_when_the_swap_fails(run_env):
    root, work, args, log = run_env
    (Path(args.staged) / "lib" / "app" / "main.py").unlink()  # an incomplete release
    assert au.run(args) == 1
    assert au.version_of(root) == "2.0.0" and log["trays"] == ["2.0.0"] and log["synced"] == []
    p = au.read_json(work / "progress.json")
    assert p["phase"] == "failed" and "Still on 2.0.0" in p["text"]


def test_run_kills_a_tray_that_doesnt_exit(run_env, monkeypatch):
    root, work, args, log = run_env
    waits = []
    monkeypatch.setattr(au, "wait_for_exit", lambda pid, timeout: waits.append(pid) or len(waits) > 1)
    assert au.run(args) == 0
    assert log["killed"] == [4242]


def test_launch_starts_a_copy_isolated(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(au, "_popen_detached", lambda cmd, cwd: started.append((cmd, cwd)) or FakeProc(1))
    au.launch(python=Path("C:/py/pythonw.exe"), work=tmp_path / "updates", root=tmp_path / "root",
              data=tmp_path / "data", staged=tmp_path / "s", version="2.1.0", from_version="2.0.0", pid=7,
              url="http://127.0.0.1:8000/")
    cmd, cwd = started[0]
    copy = tmp_path / "updates" / "apply_update.py"
    assert copy.read_text(encoding="utf-8") == Path(au.__file__).read_text(encoding="utf-8")
    assert cmd[:4] == [str(Path("C:/py/pythonw.exe")), "-I", "-S", str(copy)]
    assert cmd[cmd.index("--pid") + 1] == "7" and cmd[cmd.index("--version") + 1] == "2.1.0"
    assert cwd == tmp_path / "updates"


def test_the_new_tray_starts_from_lib_in_the_runtime_venv(install, monkeypatch):
    root, work, staged = install
    started = []
    monkeypatch.setattr(au, "_popen_detached", lambda cmd, cwd: started.append((cmd, cwd)) or FakeProc(1))
    au.start_tray(root)
    cmd, cwd = started[0]
    assert cmd == [str(root / "runtime" / "venv" / "Scripts" / "pythonw.exe"), "-m", "app.desktop.tray", "--background"]
    assert cwd == root / "lib"  # where `app` is imported from


def test_an_update_holds_the_mutex_the_exe_waits_on(tmp_path, monkeypatch):
    """IndiePulse.exe started while an update runs waits for it (launcher/Instance.cs, UpdateRunning)."""
    import ctypes
    if not hasattr(ctypes, "windll"):
        pytest.skip("Windows")
    monkeypatch.delenv("INDIEPULSE_INSTANCE", raising=False)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    k32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    name = au.updating_mutex_name(tmp_path)
    assert name.startswith("Local\\IndiePulse-") and name.endswith("-updating")
    assert not k32.OpenMutexW(0x00100000, False, name)
    held = au.hold_updating_mutex(tmp_path)
    try:
        seen = k32.OpenMutexW(0x00100000, False, name)
        assert seen
        k32.CloseHandle(seen)
    finally:
        k32.CloseHandle(held)
    monkeypatch.setenv("INDIEPULSE_INSTANCE", "0123456789abcdef")  # the exe's own key wins
    assert au.updating_mutex_name(tmp_path) == "Local\\IndiePulse-0123456789abcdef-updating"


# ── wait_healthy against a real little server ────────────────────────────────
@pytest.fixture
def fake_server():
    answers = {"version": "2.1.0", "health": 200}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                code, body = answers["health"], {"status": "ok"}
            elif self.path == "/version" and answers["version"]:
                code, body = 200, {"app_version": answers["version"]}
            else:
                code, body = 404, {}
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", answers
    server.shutdown()


def test_wait_healthy_wants_the_new_version(fake_server):
    url, answers = fake_server
    assert au.wait_healthy(url, "2.1.0", FakeProc(1), timeout=5, interval=0.1) == (True, "")
    answers["version"] = "2.0.0"  # an old server still holds the port
    ok, why = au.wait_healthy(url, "2.1.0", FakeProc(1), timeout=0.5, interval=0.1)
    assert not ok and "is 2.0.0, not 2.1.0" in why
    answers["version"] = None     # no /version at all: /health is enough
    assert au.wait_healthy(url, "2.1.0", FakeProc(1), timeout=5, interval=0.1)[0]


def test_wait_healthy_fails_fast_when_the_tray_exits(fake_server):
    url, answers = fake_server
    answers["health"] = 503
    proc = FakeProc(1)
    proc.returncode = 1
    ok, why = au.wait_healthy(url, "2.1.0", proc, timeout=30, interval=0.1)
    assert not ok and "exited" in why
