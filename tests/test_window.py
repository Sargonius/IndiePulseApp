"""IndiePulse's own window (app/desktop/window.py) and what IndiePulse.exe and the tray say to each other
(app/desktop/winapi.py): how it is started, the named events, what an older IndiePulse left behind. Nothing is started
for real here and the registry isn't touched: the browser, the shortcuts and the autostart entry are faked."""
import ctypes
import threading
import uuid
from pathlib import Path

import pytest

from app.desktop import window, winapi


@pytest.fixture
def no_window(monkeypatch, tmp_path):
    """No IndiePulse window open; what would be started is recorded."""
    started, opened = [], []
    monkeypatch.setattr(window, "PROFILE", tmp_path / "browser")
    monkeypatch.setattr(window, "bring_to_front", lambda: False)
    monkeypatch.setattr(window, "_start", lambda cmd: started.append(cmd))
    monkeypatch.setattr(window.webbrowser, "open", lambda url: opened.append(url))
    return started, opened


def test_the_window_is_edge_in_app_mode_with_a_profile_of_its_own(no_window, monkeypatch, tmp_path):
    started, opened = no_window
    monkeypatch.setattr(window, "find_browser", lambda: r"C:\Edge\msedge.exe")
    assert window.open_window("http://127.0.0.1:8790/") == "app"
    cmd = started[-1]
    assert cmd[0] == r"C:\Edge\msedge.exe" and "--app=http://127.0.0.1:8790/" in cmd
    assert f"--user-data-dir={tmp_path / 'browser'}" in cmd and "--no-first-run" in cmd
    assert any(a.startswith("--window-size=") for a in cmd)  # the first window only: Edge remembers it after that
    (tmp_path / "browser").mkdir()
    window.open_window("http://127.0.0.1:8790/")
    assert not any(a.startswith("--window-size=") for a in started[-1]) and not opened


def test_an_open_window_comes_to_the_front_instead_of_another(no_window, monkeypatch):
    started, opened = no_window
    monkeypatch.setattr(window, "bring_to_front", lambda: True)
    monkeypatch.setattr(window, "find_browser", lambda: r"C:\Edge\msedge.exe")
    assert window.open_window("http://127.0.0.1:8790/") == "front" and not started
    # a link from the tray menu opens a window of its own anyway
    assert window.open_window("http://127.0.0.1:8790/api/accounts/connect/youtube/go", new=True) == "app"
    assert "--app=http://127.0.0.1:8790/api/accounts/connect/youtube/go" in started[-1]


def test_without_edge_or_chrome_the_default_browser(no_window, monkeypatch, tmp_path):
    started, opened = no_window
    monkeypatch.setattr(window, "find_browser", lambda: None)
    assert window.open_window("http://127.0.0.1:8790/") == "browser"
    assert opened == ["http://127.0.0.1:8790/"] and not started


def test_indiepulse_browser_picks_the_browser(monkeypatch, tmp_path):
    monkeypatch.setenv("INDIEPULSE_BROWSER", "default")
    assert window.find_browser() is None
    chromium = tmp_path / "chrome.exe"
    chromium.write_bytes(b"MZ")
    monkeypatch.setenv("INDIEPULSE_BROWSER", str(chromium))
    assert window.find_browser() == str(chromium)
    monkeypatch.delenv("INDIEPULSE_BROWSER")
    found = window.find_browser()
    assert found is None or Path(found).name.lower() in ("msedge.exe", "chrome.exe")


# ── IndiePulse.exe ↔ the tray ────────────────────────────────────────────────
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenEventW.restype = ctypes.c_void_p
_k32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
_k32.CreateEventW.restype = ctypes.c_void_p
_k32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
_k32.SetEvent.argtypes = [ctypes.c_void_p]
_k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
_k32.CloseHandle.argtypes = [ctypes.c_void_p]


def test_the_exe_finds_the_tray_and_asks_it_for_the_window_and_to_quit(monkeypatch):
    monkeypatch.setenv("INDIEPULSE_INSTANCE", uuid.uuid4().hex[:16])  # what IndiePulse.exe passes the tray
    name = winapi.event_name("show")
    assert not _k32.OpenEventW(0x00100000 | 0x0002, False, name)  # not running: the exe starts it
    shown, quit_ = threading.Event(), threading.Event()
    events = winapi.InstanceEvents()
    events.listen(on_show=shown.set, on_quit=quit_.set)
    h = _k32.OpenEventW(0x00100000 | 0x0002, False, name)  # running: the exe sets "show"
    assert h
    _k32.SetEvent(h)
    _k32.CloseHandle(h)
    assert shown.wait(5) and not quit_.is_set()
    h = _k32.OpenEventW(0x0002, False, winapi.event_name("quit"))  # the uninstaller
    _k32.SetEvent(h)
    _k32.CloseHandle(h)
    assert quit_.wait(5)


def test_ready_reaches_the_exe_that_waits(monkeypatch):
    monkeypatch.setenv("INDIEPULSE_INSTANCE", uuid.uuid4().hex[:16])
    assert winapi.InstanceEvents.signal_ready() is False  # nobody waiting: nothing to do
    ready = _k32.CreateEventW(None, True, False, winapi.event_name("ready"))  # IndiePulse.exe's, before it starts us
    try:
        assert winapi.InstanceEvents.signal_ready() is True
        assert _k32.WaitForSingleObject(ready, 0) == 0
    finally:
        _k32.CloseHandle(ready)


def test_shortcuts_and_autostart_start_the_exe(monkeypatch, tmp_path):
    monkeypatch.setattr(winapi, "HOME", tmp_path)
    exe, args = winapi.launch_command(background=True)
    assert Path(exe).name == "pythonw.exe" and args.endswith('launch.py" --background')  # a checkout without the exe
    (tmp_path / "IndiePulse.exe").write_bytes(b"MZ")
    assert winapi.launch_command() == (str(tmp_path / "IndiePulse.exe"), "")
    assert winapi.launch_command(background=True) == (str(tmp_path / "IndiePulse.exe"), "--background")


def test_what_an_older_indiepulse_left_starts_the_exe_now(monkeypatch, tmp_path):
    """The owner's autostart entry and shortcuts pointed at .venv\\Scripts\\pythonw.exe IndiePulse.pyw, which is gone."""
    root = Path(winapi.ROOT)
    monkeypatch.setattr(winapi, "HOME", tmp_path)
    (tmp_path / "IndiePulse.exe").write_bytes(b"MZ")
    entry = [f'"{root}\\.venv\\Scripts\\pythonw.exe" "{root}\\IndiePulse.pyw" --background']
    monkeypatch.setattr(winapi, "autostart_command", lambda: entry[0])
    monkeypatch.setattr(winapi, "set_autostart", lambda on: entry.__setitem__(0, winapi._command_line(True)))
    assert winapi.refresh_autostart()
    assert entry[0] == f'"{tmp_path / "IndiePulse.exe"}" --background'
    assert not winapi.refresh_autostart()  # up to date
    entry[0] = r'"D:\Other\IndiePulse\IndiePulse.exe" --background'  # another copy's: not ours to change
    assert not winapi.refresh_autostart()

    made = []
    desktop, start = tmp_path / "Desktop" / "IndiePulse.lnk", tmp_path / "Start" / "IndiePulse.lnk"
    desktop.parent.mkdir()
    desktop.write_bytes(b"lnk")  # the start-menu one was deleted by the user: not made again
    monkeypatch.setattr(winapi, "shortcut_paths", lambda: [desktop, start])
    monkeypatch.setattr(winapi, "create_shortcuts", lambda icon, paths=None: made.append(paths))
    marker = tmp_path / "indiepulse-shortcuts"
    assert winapi.refresh_shortcuts(marker, tmp_path / "x.ico") == []  # no marker: the tray never made any
    marker.write_text("", encoding="utf-8")  # an older tray's marker: empty
    assert winapi.refresh_shortcuts(marker, tmp_path / "x.ico") == [desktop] and made == [[desktop]]
    assert marker.read_text(encoding="utf-8") == winapi.shortcuts_stamp()
    assert winapi.refresh_shortcuts(marker, tmp_path / "x.ico") == []


def test_the_uninstall_entry_follows_the_version(monkeypatch):
    monkeypatch.setattr(winapi, "installed_here", lambda: False)
    assert winapi.refresh_uninstall_entry("2.1.0") is False  # not an installed copy: no entry of its own
    assert winapi._same_path("C:\\Users\\me\\AppData\\Local\\Programs\\IndiePulse\\",
                             Path("C:/Users/me/AppData/Local/Programs/IndiePulse"))
