"""IndiePulse's own window (app/desktop/window.py) — its browser, its profiles, its links to other sites
(/api/system/open_external) — and what IndiePulse.exe and the tray say to each other (app/desktop/winapi.py): how it is
started, the named events, what an older IndiePulse left behind. No browser is started here and the registry isn't
written: the browser, the default browser's registration, the shortcuts and the autostart entry are faked."""
import ctypes
import os
import re
import subprocess
import sys
import threading
import uuid
import winreg
from pathlib import Path

import psutil
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
    # through the start URL, which marks the window (the cookie) and goes on to the page
    assert cmd[0] == r"C:\Edge\msedge.exe" and "--app=http://127.0.0.1:8790/api/system/app_window?next=%2F" in cmd
    assert f"--user-data-dir={tmp_path / 'browser'}" in cmd and "--no-first-run" in cmd
    assert any(a.startswith("--window-size=") for a in cmd)  # the first window only: Edge remembers it after that
    (tmp_path / "browser").mkdir()
    window.open_window("http://127.0.0.1:8790/")
    assert not any(a.startswith("--window-size=") for a in started[-1]) and not opened


def test_chrome_gets_a_profile_of_its_own_never_edges(no_window, monkeypatch, tmp_path):
    """The owner's Edge profile (browser\\) keeps its sign-ins and stays Edge's: Chrome, the default browser now,
    starts with browser-chrome\\ — its first window too, though Edge's profile is there."""
    started, _ = no_window
    (tmp_path / "browser").mkdir()
    monkeypatch.setattr(window, "find_browser", lambda: r"C:\Google\Chrome\Application\chrome.exe")
    assert window.open_window("http://127.0.0.1:8790/") == "app"
    cmd = started[-1]
    assert f"--user-data-dir={tmp_path / 'browser-chrome'}" in cmd
    assert not any(a == f"--user-data-dir={tmp_path / 'browser'}" for a in cmd)
    assert any(a.startswith("--window-size=") for a in cmd)
    assert window.profile(r"C:\Edge\msedge.exe") == tmp_path / "browser"
    assert window.profile(r"C:\Brave\Application\brave.exe") == tmp_path / "browser-brave"
    assert window.profile(r"C:\Vivaldi\Application\VIVALDI.EXE") == tmp_path / "browser-vivaldi"


def test_an_open_window_comes_to_the_front_instead_of_another(no_window, monkeypatch):
    started, opened = no_window
    monkeypatch.setattr(window, "bring_to_front", lambda: True)
    monkeypatch.setattr(window, "find_browser", lambda: r"C:\Edge\msedge.exe")
    assert window.open_window("http://127.0.0.1:8790/") == "front" and not started
    # a link from the tray menu opens a window of its own anyway
    assert window.open_window("http://127.0.0.1:8790/api/accounts/connect/youtube/go", new=True) == "app"
    assert ("--app=http://127.0.0.1:8790/api/system/app_window?next=%2Fapi%2Faccounts%2Fconnect%2Fyoutube%2Fgo"
            in started[-1])


def test_the_start_url_goes_on_to_the_page():
    assert window.start_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000/api/system/app_window?next=%2F"
    assert window.start_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000/api/system/app_window?next=%2F"
    assert (window.start_url("http://localhost:8870/posts?status=failed")
            == "http://localhost:8870/api/system/app_window?next=%2Fposts%3Fstatus%3Dfailed")


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
    found = window.find_browser()  # this computer's, read for real
    assert found is None or window.has_app_mode(found)


# ── which browser: the default one when it has app mode ─────────────────────
@pytest.fixture
def registry(monkeypatch, tmp_path):
    """A registry of our own: {(hive, key, value name): data}; the browsers' exes exist in tmp_path. Edge is where
    App Paths say; nothing in Program Files."""
    values = {}
    monkeypatch.setattr(window, "_reg_value", lambda hive, sub, name="": values.get((hive, sub, name), ""))
    monkeypatch.delenv("INDIEPULSE_BROWSER", raising=False)
    exes = {}
    for key, rel in (("msedge", r"Microsoft\Edge\Application\msedge.exe"),
                     ("chrome", r"Google\Chrome\Application\chrome.exe"), ("brave", r"BraveSoftware\brave.exe"),
                     ("yandex", r"Yandex\YandexBrowser\Application\browser.exe"),
                     ("firefox", r"Mozilla Firefox\firefox.exe"), ("other", r"Some\browser.exe")):
        exes[key] = tmp_path / rel
        exes[key].parent.mkdir(parents=True, exist_ok=True)
        exes[key].write_bytes(b"MZ")
    monkeypatch.setattr(window, "_app_path", lambda exe: str(exes["msedge"]) if exe == "msedge.exe" else None)
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(tmp_path / "nowhere"))

    def default(prog_id, command, hive=winreg.HKEY_LOCAL_MACHINE):
        values[(winreg.HKEY_CURRENT_USER, window._USER_CHOICE, "ProgId")] = prog_id
        key = rf"{prog_id}\shell\open\command"
        values[(hive, key if hive == winreg.HKEY_CLASSES_ROOT else rf"Software\Classes\{key}", "")] = command
    return default, exes, values


def test_the_default_browser_opens_the_window_when_it_is_chromium(registry):
    default, exes, _ = registry
    default("ChromeHTML", f'"{exes["chrome"]}" --single-argument %1')
    assert window.default_browser() == str(exes["chrome"])
    assert window.find_browser() == str(exes["chrome"])
    default("BraveHTML", f'{exes["brave"]} --single-argument %1')  # unquoted: the program is up to its .exe
    assert window.find_browser() == str(exes["brave"])
    default("YandexHTML", f'"{exes["yandex"]}" -- "%1"')
    assert window.find_browser() == str(exes["yandex"])


def test_firefox_or_an_unknown_default_leaves_the_window_to_edge(registry):
    default, exes, _ = registry
    default("FirefoxURL-308046B0AF4A39CB", f'"{exes["firefox"]}" -osint -url "%1"')
    assert window.default_browser() == str(exes["firefox"])
    assert window.find_browser() == str(exes["msedge"])
    default("SomeHTML", f'"{exes["other"]}" "%1"')  # a browser.exe that isn't Yandex's
    assert window.find_browser() == str(exes["msedge"])
    default("GoneHTML", r'"C:\Gone\chrome.exe" --single-argument %1')  # its program was uninstalled
    assert window.default_browser() is None and window.find_browser() == str(exes["msedge"])


def test_the_users_own_registration_comes_first(registry):
    default, exes, values = registry
    default("ChromeHTML", f'"{exes["chrome"]}" --single-argument %1', hive=winreg.HKEY_CLASSES_ROOT)
    assert window.default_browser() == str(exes["chrome"])  # only the merged view has it
    values[(winreg.HKEY_CURRENT_USER, r"Software\Classes\ChromeHTML\shell\open\command", "")] = \
        f'"{exes["brave"]}" --single-argument %1'
    assert window.default_browser() == str(exes["brave"])


def test_indiepulse_browser_wins_over_the_default(registry, monkeypatch):
    default, exes, _ = registry
    default("ChromeHTML", f'"{exes["chrome"]}" --single-argument %1')
    monkeypatch.setenv("INDIEPULSE_BROWSER", str(exes["msedge"]))
    assert window.find_browser() == str(exes["msedge"])
    monkeypatch.setenv("INDIEPULSE_BROWSER", "default")
    assert window.find_browser() is None


# ── the window's processes: any of its profiles ──────────────────────────────
class _Proc:
    def __init__(self, pid, name, cmdline):
        self.pid, self.info, self._cmdline = pid, {"name": name}, cmdline

    def cmdline(self):
        return self._cmdline


def test_the_window_is_found_in_either_profile(monkeypatch, tmp_path):
    """An Edge window left from before the default browser became Chrome is found (and closed on Quit) as well as
    Chrome's; the user's own Chrome and another IndiePulse's window are not."""
    monkeypatch.setattr(window, "PROFILE", tmp_path / "browser")
    monkeypatch.delenv("INDIEPULSE_BROWSER", raising=False)
    procs = [
        _Proc(1, "msedge.exe", ["msedge.exe", "--app=http://127.0.0.1/", f"--user-data-dir={tmp_path / 'browser'}"]),
        _Proc(2, "chrome.exe", ["chrome.exe", "--type=renderer", f"--user-data-dir={tmp_path / 'BROWSER-CHROME'}"]),
        _Proc(3, "chrome.exe", ["chrome.exe", "--profile-directory=Default"]),  # the user's own browsing
        _Proc(4, "chrome.exe", ["chrome.exe", f"--user-data-dir={tmp_path / 'other' / 'browser-chrome'}"]),
        _Proc(5, "brave.exe", ["brave.exe", f"--user-data-dir={tmp_path / 'browser-brave'}"]),
        _Proc(6, "python.exe", ["python.exe", f"--user-data-dir={tmp_path / 'browser'}"]),  # not a browser
        _Proc(7, "chrome.exe", ["chrome.exe", f"--user-data-dir={tmp_path / 'browser-chrome' / 'sub'}"]),
        _Proc(8, "chrome.exe", ["chrome.exe", f"--user-data-dir={tmp_path / 'browser-'}"]),
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs=None: iter(procs))
    assert window._profile_pids() == {1, 2, 5}
    assert window.is_profile(f'"{tmp_path / "browser-chrome"}"') and not window.is_profile("")


def test_the_uninstaller_knows_the_same_browsers():
    """launcher/Installer.cs looks for leftovers of the window among the same exes (KillLeftovers)."""
    cs = (Path(__file__).resolve().parents[1] / "launcher" / "Installer.cs").read_text(encoding="utf-8")
    listed = re.search(r"string\[\] Browsers = \{([^}]*)\}", cs).group(1)
    assert sorted(re.findall(r'"([^"]+)"', listed)) == sorted(window._APP_MODE)


_CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
_HARNESS = r"""
using System;
using System.IO;
namespace IndiePulse
{
    static class ProfileCheck
    {
        static void Main(string[] args)
        {
            var home = new Home(args[0]);
            foreach (string line in File.ReadAllLines(args[1])) Console.WriteLine(home.UsesBrowserProfile(line) ? "1" : "0");
        }
    }
}
"""


@pytest.mark.skipif(not _CSC.is_file(), reason="needs the C# compiler of Windows")
def test_the_uninstaller_matches_the_profiles_as_the_tray_does(tmp_path, monkeypatch):
    """Home.UsesBrowserProfile (the uninstaller's KillLeftovers) and window.is_profile agree, on command lines as
    Windows shows them: an argument quoted whole, or Chrome's children's --user-data-dir="…"."""
    data = tmp_path.resolve() / "data dir" / "IndiePulse"  # long names: the exe spells out 8.3 ones of what exists
    monkeypatch.setattr(window, "PROFILE", data / "browser")
    monkeypatch.setenv("INDIEPULSE_DATA", str(data))
    cases = [(["msedge.exe", f"--user-data-dir={data / 'browser'}", "--no-first-run"], True),
             (["chrome.exe", "--type=renderer", f"--user-data-dir={data / 'browser-chrome'}"], True),
             (["chrome.exe", f"--user-data-dir={data / 'browserx'}"], False),
             (["chrome.exe", f"--user-data-dir={data / 'browser-'}"], False),
             (["chrome.exe", f"--user-data-dir={data / 'browser-chrome' / 'Default'}"], False),
             (["chrome.exe", f"--user-data-dir={data}-test\\browser"], False),
             (["chrome.exe", "https://example.com/browser"], False)]
    for argv, ours in cases:
        assert window._has_profile(argv) is ours, argv
    lines = [subprocess.list2cmdline(argv) for argv, _ in cases]
    lines.append(f'"C:\\Chrome\\chrome.exe" --type=gpu-process --user-data-dir="{data / "browser-chrome"}" --x')
    (tmp_path / "cases.txt").write_text("\n".join(lines), encoding="utf-8")
    src = tmp_path / "ProfileCheck.cs"
    src.write_text(_HARNESS, encoding="utf-8")
    launcher = Path(__file__).resolve().parents[1] / "launcher"
    exe = tmp_path / "check.exe"
    r = subprocess.run([str(_CSC), "-nologo", f"-out:{exe}", str(src), str(launcher / "Home.cs"),
                        str(launcher / "DriveSettings.cs")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    out = subprocess.run([str(exe), str(tmp_path / "IndiePulse"), str(tmp_path / "cases.txt")],
                         capture_output=True, text=True, timeout=60).stdout.split()
    assert out == ["1" if ours else "0" for _, ours in cases] + ["1"]


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
    monkeypatch.setattr(winapi, "shortcut_targets", lambda paths: {p: str(tmp_path / "IndiePulse.pyw") for p in paths})
    assert winapi.refresh_shortcuts(marker, tmp_path / "x.ico") == [desktop] and made == [[desktop]]
    assert marker.read_text(encoding="utf-8") == winapi.shortcuts_stamp()
    assert winapi.refresh_shortcuts(marker, tmp_path / "x.ico") == []


def test_shortcuts_of_another_copy_stay(tmp_path, monkeypatch):
    """A second copy on the drive (a test one) doesn't take over the shortcuts of the one that is there: a run of its
    own leaves them; one whose program is gone (or under another day's drive letter), or this copy's, it may make."""
    home = tmp_path / "IndiePulse"
    other = tmp_path / "IndiePulse-test"  # its name starts like this copy's: a folder, not a string prefix
    for d in (home, other):
        d.mkdir()
        (d / "IndiePulse.exe").write_bytes(b"MZ")
    monkeypatch.setattr(winapi, "HOME", home)
    monkeypatch.setattr(winapi, "ROOT", home)
    desktop, start, music = (tmp_path / n for n in ("desktop.lnk", "start.lnk", "music.lnk"))
    for p in (desktop, start):
        p.write_bytes(b"lnk")
    targets = {desktop: str(other / "IndiePulse.exe"), start: str(home / "IndiePulse.exe")}
    monkeypatch.setattr(winapi, "shortcut_targets", lambda paths: {p: targets[p] for p in paths if p in targets})

    free, taken = winapi.free_shortcut_paths([desktop, start, music])
    assert free == [start, music] and taken == {desktop: str(other / "IndiePulse.exe")}

    targets[desktop] = r"Q:\Gone\IndiePulse\IndiePulse.exe"  # that copy is gone (or the drive had another letter)
    assert winapi.free_shortcut_paths([desktop])[0] == [desktop]
    targets[desktop] = str(home / ".venv" / "Scripts" / "pythonw.exe")  # this copy's, the old way: its own
    assert winapi.free_shortcut_paths([desktop])[0] == [desktop]


def test_the_uninstall_entry_follows_the_version(monkeypatch):
    monkeypatch.setattr(winapi, "installed_here", lambda: False)
    assert winapi.refresh_uninstall_entry("2.1.0") is False  # not an installed copy: no entry of its own
    assert winapi._same_path("C:\\Users\\me\\AppData\\Local\\Programs\\IndiePulse\\",
                             Path("C:/Users/me/AppData/Local/Programs/IndiePulse"))


# ── the window's links to other sites ────────────────────────────────────────
SAME_ORIGIN = {"Origin": "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-origin"}  # what the window's fetch sends


@pytest.fixture
def opened(monkeypatch):
    """What /api/system/open_external would open: recorded, never opened (no test starts a browser)."""
    urls = []
    monkeypatch.setattr(window, "open_external", urls.append)
    return urls


def test_the_start_url_marks_the_window_and_goes_on(client):
    r = client.get("/api/system/app_window?next=%2Fposts%3Fstatus%3Dfailed", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/posts?status=failed"
    cookie = r.headers["set-cookie"].lower()
    assert cookie.startswith("ip_window=app;") and "path=/" in cookie and "samesite=lax" in cookie
    assert "max-age=34560000" in cookie and "httponly" in cookie
    for elsewhere in ("//evil.example/", "/\\evil.example", "https://evil.example/", "posts", "/\t/evil.example"):
        r = client.get("/api/system/app_window", params={"next": elsewhere}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/", elsewhere  # only a page of this app


def test_only_the_app_window_gets_the_link_script(client):
    assert "/api/system/open_external" not in client.get("/").text  # a tab of the user's browser: nothing changes
    client.get("/api/system/app_window?next=/")  # what the window opens first: the cookie stays
    page = client.get("/").text
    assert "/api/system/open_external" in page and "auxclick" in page and "window.open = function" in page
    assert "/api/system/open_external" in client.get("/posts").text  # every page (base.html)


@pytest.mark.parametrize("url", [
    "https://discord.gg/uYHm3aU2W4", "HTTPS://PAYPAL.ME/sargoniusgames", "http://example.com/a?b=c#d",
    "https://mail.google.com/mail/?view=cm&fs=1&to=a%40b.example&su=Hi&body=" + "%D0%BF" * 2000,
    "mailto:creator@example.com?subject=Hello%20there", "https://studio.youtube.com/channel/UC1/videos"])
def test_links_to_other_sites_open_in_the_default_browser(client, opened, url):
    r = client.post("/api/system/open_external", json={"url": url}, headers=SAME_ORIGIN)
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert opened == [url]


@pytest.mark.parametrize("url", [
    "file:///C:/", "javascript:alert(1)", "JavaScript:alert(1)", r"C:\Windows\notepad.exe", r"\\server\share",
    "/posts", "posts", "ms-settings:privacy", "data:text/html,hi", "vbscript:x", "about:blank", "https://",
    "http:///etc/passwd", "https://example.com/a b", 'https://example.com/"--foo', " https://example.com/",
    "https://example.com/\r\nx", "https://example.com/\x00", "https://ex\u0430mple.com/", "https://[::1/", "",
    "https://example.com/" + "a" * 20000])
def test_anything_else_is_refused(client, opened, url):
    r = client.post("/api/system/open_external", json={"url": url}, headers=SAME_ORIGIN)
    assert r.status_code == 400 and r.json()["detail"]
    assert not opened


def test_a_cross_site_page_cant_open_links(client, opened):
    """README → Security notes: other websites can't drive IndiePulse from the user's browser
    (main.block_cross_site_writes) — this endpoint neither."""
    for headers in ({"Origin": "https://evil.example"}, {"Sec-Fetch-Site": "cross-site"}, {"Origin": "null"},
                    {"Origin": "http://127.0.0.1:9999", "Sec-Fetch-Site": "same-site"}):
        r = client.post("/api/system/open_external", json={"url": "https://evil.example/"}, headers=headers)
        assert r.status_code == 403, headers
    r = client.post("/api/system/open_external", content='{"url": "https://evil.example/"}',  # a form: no preflight
                    headers={"Origin": "https://evil.example", "Content-Type": "text/plain"})
    assert r.status_code == 403
    assert client.get("/api/system/open_external", params={"url": "https://evil.example/"}).status_code == 405
    assert not opened


def test_a_link_windows_cant_open_says_why_and_only_the_site_is_logged(client, monkeypatch):
    from loguru import logger

    def no_program(url):
        raise OSError(1155, "No application is associated with the specified file", url)
    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        monkeypatch.setattr(window, "open_external", no_program)
        r = client.post("/api/system/open_external", headers=SAME_ORIGIN,
                        json={"url": "https://studio.youtube.com/channel/UC123/videos?token=secret"})
        assert r.status_code == 500 and "No application is associated" in r.json()["detail"]
        monkeypatch.setattr(window, "open_external", lambda url: None)
        client.post("/api/system/open_external", json={"url": "mailto:someone@example.com"}, headers=SAME_ORIGIN)
        client.post("/api/system/open_external", json={"url": "javascript:alert('someone')"}, headers=SAME_ORIGIN)
    finally:
        logger.remove(sink)
    log = "".join(lines)
    assert "studio.youtube.com" in log and "mailto" in log
    assert "UC123" not in log and "secret" not in log and "someone" not in log


def test_a_link_leaves_indiepulses_job(monkeypatch):
    """The server is in the tray's kill-on-close job, and Windows starts a browser that isn't running from the
    process that asks: it would close with IndiePulse. A pythonw outside the job asks instead."""
    started, startfile, codes = [], [], [0]

    class Proc:
        def __init__(self, cmd, creationflags=0, **kw):
            started.append((cmd, creationflags))

        def wait(self, timeout=None):
            return codes[0]
    monkeypatch.setattr(window, "_in_job", lambda: True)
    monkeypatch.setattr(window.subprocess, "Popen", Proc)
    monkeypatch.setattr(window.os, "startfile", startfile.append, raising=False)
    window.open_external("https://discord.gg/x")
    cmd, flags = started[-1]
    assert cmd[1:] == ["-I", "-c", window._OPEN, "https://discord.gg/x"] and flags & window._BREAKAWAY
    assert Path(cmd[0]).name.lower() in ("pythonw.exe", "python.exe") and not startfile
    codes[0] = 1  # the helper's os.startfile failed
    with pytest.raises(OSError):
        window.open_external("https://discord.gg/x")

    def denied(cmd, **kw):
        raise PermissionError(5, "Access is denied")
    monkeypatch.setattr(window.subprocess, "Popen", denied)  # a job that doesn't let it go: from the server then
    window.open_external("https://discord.gg/x")
    assert startfile == ["https://discord.gg/x"]
    monkeypatch.setattr(window, "_in_job", lambda: False)  # a server started by hand
    window.open_external("mailto:a@b.example")
    assert startfile[-1] == "mailto:a@b.example" and len(started) == 2
    compile(window._OPEN, "<open_external>", "exec")


def test_a_process_of_indiepulses_job_can_start_one_outside_it():
    """What open_external relies on: the job lets a process started with CREATE_BREAKAWAY_FROM_JOB go."""
    python = getattr(sys, "_base_executable", sys.executable)  # not the venv's: it starts the real one as a child
    try:
        subprocess.run([python, "-c", "pass"], creationflags=window._BREAKAWAY, check=True, timeout=60)
    except OSError:
        pytest.skip("the tests run in a job that keeps its processes")
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = ctypes.c_void_p
    k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k32.IsProcessInJob.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    def in_job(pid: int) -> bool:
        handle, inside = k32.OpenProcess(0x1000, False, pid), ctypes.c_int()  # PROCESS_QUERY_LIMITED_INFORMATION
        assert handle and k32.IsProcessInJob(handle, job._handle, ctypes.byref(inside))
        k32.CloseHandle(handle)
        return bool(inside.value)

    job = winapi.KillOnCloseJob()
    # waits until it is in the job, then starts one as open_external does
    code = ("import subprocess, sys; sys.stdin.readline(); "
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], creationflags=0x01000000); "
            "print(p.pid, flush=True)")
    child = subprocess.Popen([python, "-c", code], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    grandchild = None
    try:
        assert job.assign(child)
        child.stdin.write("\n")
        child.stdin.flush()
        grandchild = int(child.stdout.readline())
        assert in_job(child.pid) and not in_job(grandchild)
    finally:
        child.kill()
        if grandchild:
            psutil.Process(grandchild).kill()


