"""IndiePulse on the drive the user carries between computers: a .venv from another one, packages from the lock,
paths saved when the drive had another letter, a club computer that shouldn't get shortcuts; a release's layout
(IndiePulse.exe + lib\\, its runtime\\) and where its data goes."""
from pathlib import Path

from app.core import paths
from app.desktop import apply_update, deps, winapi


def _venv(root: Path, home: Path) -> Path:
    venv = root / ".venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_bytes(b"")
    (venv / "pyvenv.cfg").write_text(f"home = {home}\nversion_info = 3.12\n", encoding="utf-8")
    return venv


def test_a_venv_from_another_computer_is_dead(monkeypatch, tmp_path):
    here = tmp_path / "python"
    here.mkdir()
    (here / "python.exe").write_bytes(b"")
    monkeypatch.setattr(deps, "VENV", _venv(tmp_path / "a", here))
    assert deps.venv_alive()
    # made at home: its base Python is in another user's profile (or on the drive under another letter)
    monkeypatch.setattr(deps, "VENV", _venv(tmp_path / "b", Path(r"C:\Users\nobody-here\Python312")))
    assert not deps.venv_alive()
    monkeypatch.setattr(deps, "VENV", tmp_path / "none" / ".venv")
    assert not deps.venv_alive()


def test_uv_keeps_its_pythons_on_the_drive(monkeypatch, tmp_path):
    kit = tmp_path / "ClubKit"
    (kit / "tools" / "uv").mkdir(parents=True)
    (kit / "tools" / "uv" / "uv.exe").write_bytes(b"")
    monkeypatch.setattr(deps, "KIT", kit)
    monkeypatch.setattr(deps, "RUNTIME", tmp_path / "no-runtime")
    monkeypatch.setattr(deps.shutil, "which", lambda name: r"C:\Tools\uv.exe")
    for k in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_PREFERENCE"):
        monkeypatch.delenv(k, raising=False)
    assert deps.uv() == str(kit / "tools" / "uv" / "uv.exe")  # ClubKit's before one on PATH: the exe's own choice
    env = deps.uv_env()
    assert env["UV_PYTHON_INSTALL_DIR"] == str(kit / "data" / "uv" / "python")
    assert env["UV_CACHE_DIR"] == str(kit / "data" / "uv" / "cache") and env["UV_PYTHON_PREFERENCE"] == "only-managed"
    monkeypatch.setenv("UV_CACHE_DIR", r"X:\own")  # ClubKit's scripts already said where
    assert deps.uv_env()["UV_CACHE_DIR"] == r"X:\own"
    monkeypatch.setattr(deps, "KIT", tmp_path / "no-kit")
    assert deps.uv() == r"C:\Tools\uv.exe" and "UV_PYTHON_PREFERENCE" not in deps.uv_env()
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    assert deps.uv() is None


def test_a_release_keeps_uv_and_python_in_its_runtime(monkeypatch, tmp_path):
    """No ClubKit: IndiePulse.exe put uv into runtime\\uv and Python into runtime\\python — everything stays there
    (a portable copy leaves nothing in the profile, an uninstall takes it all), whatever the user's environment says."""
    runtime = tmp_path / "IndiePulse" / "runtime"
    (runtime / "uv").mkdir(parents=True)
    (runtime / "uv" / "uv.exe").write_bytes(b"")
    monkeypatch.setattr(deps, "KIT", tmp_path / "no-kit")
    monkeypatch.setattr(deps, "RUNTIME", runtime)
    monkeypatch.setattr(deps.shutil, "which", lambda name: r"C:\Tools\uv.exe")
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", r"C:\Users\me\uv-pythons")
    assert deps.uv() == str(runtime / "uv" / "uv.exe")
    env = deps.uv_env()
    assert env["UV_PYTHON_INSTALL_DIR"] == str(runtime / "python") and env["UV_CACHE_DIR"] == str(runtime / "cache")
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"


def test_a_release_has_its_code_in_lib(tmp_path):
    """IndiePulse.exe, README, LICENSE at the top; lib\\ the code (paths.ROOT); runtime\\venv its venv. A checkout is
    its own code folder with its .venv."""
    release = tmp_path / "IndiePulse"
    (release / "lib" / "app").mkdir(parents=True)
    (release / "lib" / "app" / "__init__.py").write_text('__version__ = "2.1.0"\n')
    assert apply_update.code_dir(release) == release / "lib" and apply_update.version_of(release) == "2.1.0"
    assert apply_update.venv_python(release) == release / "runtime" / "venv" / "Scripts" / "python.exe"
    checkout = tmp_path / "checkout"
    (checkout / "app").mkdir(parents=True)
    (checkout / "app" / "__init__.py").write_text('__version__ = "2.2.0-dev"\n')
    assert apply_update.code_dir(checkout) == checkout and apply_update.version_of(checkout) == "2.2.0-dev"
    assert apply_update.venv_python(checkout, windowed=True) == checkout / ".venv" / "Scripts" / "pythonw.exe"
    # paths.py's rule for HOME: lib\\'s parent, unless "lib" is a checkout of its own
    assert paths.HOME == paths.ROOT and not paths.is_release_layout()  # the tests run from a checkout


def test_the_lock_is_what_gets_installed(monkeypatch, tmp_path):
    """A fresh install takes the exact tested versions, not the newest `~=` allows (sqlmodel 0.0.48 broke the app)."""
    calls = []
    monkeypatch.setattr(deps, "_call", lambda cmd, log, env=None: calls.append(cmd) or True)
    monkeypatch.setattr(deps, "_STAMP", tmp_path / "stamp")
    monkeypatch.setattr(deps, "uv", lambda: None)
    assert deps.install_requirements(tmp_path / "pip.log")
    assert calls[-1][-2:] == ["-r", str(deps.LOCK)]
    assert not deps.requirements_changed()
    monkeypatch.setattr(deps, "uv", lambda: r"F:\ClubKit\tools\uv\uv.exe")
    deps.install_requirements(tmp_path / "pip.log")
    assert calls[-1][:3] == [r"F:\ClubKit\tools\uv\uv.exe", "pip", "install"] and calls[-1][-1] == str(deps.LOCK)
    locked = {line.split("==")[0].lower() for line in deps.LOCK.read_text(encoding="utf-8").splitlines()
              if "==" in line and not line.startswith("#")}
    needed = {line.split("~=")[0].split("[")[0].strip().lower() for line in deps.REQUIREMENTS.read_text(
        encoding="utf-8").splitlines() if "~=" in line and not line.startswith("#")}
    assert needed <= locked  # regenerate requirements.lock after editing requirements.txt


def test_paths_saved_under_another_drive_letter(monkeypatch, tmp_path):
    here = Path(tmp_path.anchor)  # the drive the tests run on stands for IndiePulse's drive
    monkeypatch.setattr(paths, "ROOT", here / "Work" / "IndiePulse")
    music = tmp_path / "Music"
    music.mkdir()
    away = "Q:" + str(music)[2:]  # the same folder, saved at home where the drive was Q:
    assert paths.on_this_drive(away) == str(music)
    assert paths.on_this_drive(str(music)) == str(music)
    gone = "Q:" + str(tmp_path / "nowhere")[2:]
    assert paths.on_this_drive(gone) == gone  # not here either: as it was, the caller reports it missing
    assert paths.on_this_drive("render/a.wav") == "render/a.wav" and paths.on_this_drive("") == ""


def test_music_from_home_is_found_on_the_club_drive(monkeypatch, tmp_path, session):
    from app.models.models import Preset
    from app.services import music_library, video_edit
    music = tmp_path / "SynthSocial" / "Music"
    music.mkdir(parents=True)
    (music / "CityAmbient_01.wav").write_bytes(b"RIFF")
    monkeypatch.setattr(paths, "ROOT", Path(tmp_path.anchor) / "Work" / "IndiePulse")
    away = lambda p: "Q:" + str(p)[2:]  # noqa: E731
    track = music / "CityAmbient_01.wav"
    assert music_library.folders({"music_dir": away(music)}) == [str(music)]
    assert music_library.is_allowed(str(track), cfg={"music_dir": away(music)})
    spec = video_edit.short_spec({"music": {"path": away(track)}}, 10.0)
    assert spec["music"]["path"] == str(track)
    session.add(Preset(name="ambient", music_path=away(track)))
    session.commit()
    assert str(track) in music_library.preset_music_paths(session)


def test_shortcuts_only_on_a_trusted_computer(monkeypatch, tmp_path):
    kit = tmp_path / "ClubKit"
    monkeypatch.setattr(winapi, "KIT", kit)
    monkeypatch.setattr(winapi, "PORTABLE_MARKER", tmp_path / "portable.txt")
    monkeypatch.setattr(winapi, "_machine_id", lambda: "8DB253B8-0000")
    assert winapi.trusted_pc()  # no ClubKit on the drive: an installed program
    (tmp_path / "portable.txt").write_text("")
    assert not winapi.trusted_pc()  # a portable copy: its setup promised to leave nothing on the computer
    (kit / "data").mkdir(parents=True)
    assert not winapi.trusted_pc()  # ClubKit, but no computer is trusted yet
    listed = kit / "data" / "trusted-pcs.txt"
    listed.write_text("\ufeffSARGONIUS|c72333c0-1111\n", encoding="utf-8")  # PowerShell's UTF-8 has a BOM
    assert not winapi.trusted_pc()
    listed.write_text("\ufeffSARGONIUS|c72333c0-1111\nDESKTOP-61LSSQA|8db253b8-0000\n", encoding="utf-8")
    assert winapi.trusted_pc()
    monkeypatch.setattr(winapi, "_machine_id", lambda: "")
    assert not winapi.trusted_pc()


def test_where_the_data_lives(monkeypatch, tmp_path):
    """The code can be replaced without touching the data; a copy that already runs keeps its data where it is."""
    home = tmp_path / "IndiePulse"  # a release: IndiePulse.exe here, the code in lib\\
    code = home / "lib"
    code.mkdir(parents=True)
    monkeypatch.setattr(paths, "ROOT", code)
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.delenv("INDIEPULSE_DATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    no_kit = Path(code.anchor) / "ClubKit"
    if not no_kit.is_dir():  # the drive the tests run on has no ClubKit: an installed program
        assert paths._data_dir() == tmp_path / "AppData" / "IndiePulse"
    (home / "portable.txt").write_text("", encoding="utf-8")  # the setup's Portable: data\\ next to IndiePulse.exe
    assert paths._data_dir() == home / "data"
    (code / ".settings.json").write_text("{}", encoding="utf-8")  # has been running from its own folder
    assert paths._data_dir() == code
    monkeypatch.setenv("INDIEPULSE_DATA", str(tmp_path / "elsewhere"))
    assert paths._data_dir() == tmp_path / "elsewhere"


def test_python_taken_by_an_antivirus_comes_back(monkeypatch, tmp_path):
    """Kaspersky deleted the drive's python.exe at reboot (2026-10-24): the .venv is fine, its Python only needs it back."""
    home = tmp_path / "uv-python"
    home.mkdir()
    monkeypatch.setattr(deps, "VENV", _venv(tmp_path / "app", home))  # python.exe missing in home
    calls = []

    def fake_call(cmd, log, env=None):
        calls.append(cmd)
        if cmd[1:3] == ["python", "install"]:
            (home / "python.exe").write_bytes(b"")
        return True
    monkeypatch.setattr(deps, "_call", fake_call)
    monkeypatch.setattr(deps, "uv", lambda: "uv.exe")
    assert deps.ensure_venv(tmp_path / "venv.log")
    # no rebuild; no bin shim in the profile, no registry entry
    assert calls == [["uv.exe", "python", "install", "--no-bin", "--no-registry", "--reinstall", "3.12"]]
    assert (deps.VENV / "pyvenv.cfg").exists()
