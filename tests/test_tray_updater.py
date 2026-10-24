"""The tray's updater (app/desktop/updater.py): when it checks, what update_mode does, when it installs — GitHub, the
download, the installer and the supervisor faked."""
import pytest

from app import __version__
from app.desktop import updater as tu
from app.services import updates


class FakeSup:
    def __init__(self):
        self.phase, self.quits = "running", 0

    def quit(self):
        self.quits += 1


@pytest.fixture
def env(tmp_path, monkeypatch, set_settings):
    monkeypatch.setattr(updates, "UPDATES", tmp_path / "updates")
    monkeypatch.setattr(updates, "is_git_checkout", lambda root=None: False)
    log = {"checks": 0, "downloads": [], "launches": [], "busy": [], "latest": "9.0.0"}

    def fake_check(cfg=None):
        log["checks"] += 1
        state = updates.load_state()
        state.update(checked_at=1, error="", error_code="",
                     latest={"version": log["latest"], "tag": f"v{log['latest']}", "name": "x.zip", "sha256": "0" * 64,
                             "asset_url": "https://api.github.com/a/2", "notes": ""})
        updates.save_state(state)
        return state

    def fake_download(latest, token):
        log["downloads"].append((latest["version"], token))
        if log.get("download_error"):
            raise updates.UpdateError(log["download_error"], "checksum")
        return tmp_path / "updates" / "staging" / "IndiePulse"

    def fake_launch(**kw):
        log["launches"].append(kw)
        if log.get("launch_error"):
            raise OSError("no pythonw")
    monkeypatch.setattr(updates, "check", fake_check)
    monkeypatch.setattr(updates, "download", fake_download)
    monkeypatch.setattr(tu.apply_update, "launch", fake_launch)
    monkeypatch.setattr(tu.TrayUpdater, "_spawn", lambda self, fn, *a: fn(*a))  # threads → inline
    sup, notes = FakeSup(), []
    u = tu.TrayUpdater(sup, notify=notes.append, refresh=lambda: None, base_url="http://127.0.0.1:8000")
    u.sleep = lambda s: None
    busy = log["busy"]
    monkeypatch.setattr(u, "_busy", lambda manual: busy.pop(0) if busy else "")
    set_settings(update_token="github_pat_x")
    return u, sup, notes, log, set_settings


def test_notify_mode_checks_and_tells_once_per_version(env):
    u, sup, notes, log, _ = env
    u.tick(check_due=True)
    u.tick(check_due=True)
    assert log["checks"] == 2
    assert len(notes) == 1 and "IndiePulse 9.0.0 is available" in notes[0]
    assert log["downloads"] == [] and sup.quits == 0
    assert updates.load_state()["notified"] == ["9.0.0"]  # a restarted tray doesn't tell again
    again = tu.TrayUpdater(sup, notify=notes.append, refresh=lambda: None, base_url="http://x/")
    again.check()
    assert len(notes) == 1
    assert u.can_install() and u.install_label() == "Install update v9.0.0"


def test_no_check_before_its_time_or_when_off(env):
    u, sup, notes, log, set_settings = env
    u.tick(check_due=False)
    set_settings(update_mode="off")
    u.tick(check_due=True)
    assert log["checks"] == 0 and notes == []


def test_git_checkout_never_checks_by_itself_nor_installs(env, monkeypatch):
    u, sup, notes, log, set_settings = env
    monkeypatch.setattr(updates, "is_git_checkout", lambda root=None: True)
    set_settings(update_mode="auto")
    u.tick(check_due=True)
    assert log["checks"] == 0
    u.check(manual=True)  # the menu's Check for updates still answers
    assert "git pull" in notes[-1]
    u.install()
    assert log["downloads"] == [] and log["launches"] == [] and sup.quits == 0
    assert not u.can_install()


def test_auto_mode_installs_when_nothing_is_publishing(env):
    u, sup, notes, log, set_settings = env
    set_settings(update_mode="auto")
    log["busy"].extend(["1 post being published", "1 post being published"])
    u.tick(check_due=True)
    assert log["downloads"] == [("9.0.0", "github_pat_x")]
    assert log["busy"] == []  # waited through both
    launch = log["launches"][0]
    assert launch["version"] == "9.0.0" and launch["from_version"] == __version__ and launch["pid"]
    assert sup.quits == 1  # stop server and worker, quit: the installer goes on
    assert updates.progress()["phase"] == "installing"
    assert u.installing and not u.can_install()


def test_auto_mode_doesnt_retry_a_version_that_didnt_start(env):
    u, sup, notes, log, set_settings = env
    set_settings(update_mode="auto")
    updates.set_phase("rolled_back", "9.0.0", "IndiePulse 9.0.0 didn't start, so 2.0.0 is back.")
    u.tick(check_due=True)
    assert log["downloads"] == [] and sup.quits == 0
    log["latest"] = "9.0.1"  # the fixed release is installed
    u.tick(check_due=True)
    assert log["downloads"][-1][0] == "9.0.1" and sup.quits == 1


def test_auto_mode_retries_after_a_failed_download(env):
    u, sup, notes, log, set_settings = env
    set_settings(update_mode="auto")
    log["download_error"] = "doesn't match the checksum"
    u.tick(check_due=True)
    p = updates.progress()
    assert p["phase"] == "failed" and p["stage"] == "download" and "checksum" in notes[-1]
    assert sup.quits == 0 and not u.installing
    log.pop("download_error")
    u.tick(check_due=True)
    assert sup.quits == 1


def test_install_request_from_the_web_ui(env):
    u, sup, notes, log, _ = env
    u.check()
    updates.install_request_path().write_text("9.0.0")
    u.tick(check_due=False)
    assert not updates.install_request_path().exists()
    assert log["launches"] and sup.quits == 1


def test_installer_that_cant_start_leaves_the_app_running(env):
    u, sup, notes, log, _ = env
    u.check()
    log["launch_error"] = True
    u.install()
    assert sup.quits == 0 and not u.installing
    assert updates.progress()["phase"] == "failed" and "failed" in notes[-1]


def test_manual_install_with_nothing_newer(env):
    u, sup, notes, log, _ = env
    log["latest"] = "0.0.1"
    u.check()
    u.install()
    assert notes[-1] == "There is no newer version to install." and log["downloads"] == []


def test_manual_check_reports_up_to_date_and_errors(env, monkeypatch):
    u, sup, notes, log, _ = env
    log["latest"] = "0.0.1"
    u.check(manual=True)
    assert notes[-1] == f"IndiePulse {__version__} is the latest version."
    monkeypatch.setattr(updates, "check", lambda cfg=None: {"error": "set an access token", "error_code": "need_token"})
    u.check(manual=True)
    assert notes[-1] == "Update check: set an access token"


def test_the_result_of_an_install_is_told_once(env):
    u, sup, notes, log, _ = env
    updates.set_phase("installed", __version__, f"IndiePulse updated to {__version__}.")
    u.tick(check_due=False)
    u.tick(check_due=False)
    assert notes == [f"IndiePulse updated to {__version__}."]


def test_busy_asks_the_server(monkeypatch):
    sup = FakeSup()
    u = tu.TrayUpdater(sup, notify=lambda t: None, refresh=lambda: None, base_url="http://127.0.0.1:9/")
    assert u._busy(manual=True) == ""                         # no server: the user's Install goes ahead
    assert u._busy(manual=False) == "the server to answer"    # auto waits
    sup.phase = "restarting"
    assert u._busy(manual=True) == "IndiePulse to finish starting"
