"""IndiePulse on the drive the user carries between computers: a .venv from another one, packages from the lock,
paths saved when the drive had another letter, a club computer that shouldn't get shortcuts, the drive's own
settings (IndiePulse.portable.ini); a release's layout (IndiePulse.exe + lib\\, its runtime\\) and where its data
goes."""
from pathlib import Path

from app.core import drive_settings, paths
from app.desktop import apply_update, deps, tray, winapi

UV_VARS = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_PREFERENCE")
NO_SETTINGS = drive_settings.DriveSettings()
# The settings of a drive that carries a toolkit beside IndiePulse, in the drive's root: the kit's uv, uv's cache and
# Pythons in the kit, its list of trusted computers (the folder's name is the drive owner's choice)
KIT_INI = ("portable = yes\n"
           "uv = \\Toolkit\\tools\\uv\\uv.exe\n"
           "uv_cache_dir = \\Toolkit\\data\\uv\\cache\n"
           "uv_python_dir = \\Toolkit\\data\\uv\\python\n"
           "trusted_pcs = \\Toolkit\\data\\trusted-pcs.txt\n")


def _settings(home: Path, text: str) -> drive_settings.DriveSettings:
    """The drive settings of `text`, as an IndiePulse.portable.ini next to IndiePulse.exe in `home`."""
    home.mkdir(parents=True, exist_ok=True)
    (home / drive_settings.NAME).write_text(text, encoding="utf-8")
    return drive_settings.load(home)


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
    kit = tmp_path / "kit"
    (kit / "uv").mkdir(parents=True)
    (kit / "uv" / "uv.exe").write_bytes(b"")
    home = tmp_path / "IndiePulse"
    monkeypatch.setattr(deps, "DRIVE", _settings(
        home, "uv = ..\\kit\\uv\\uv.exe\nuv_cache_dir = ..\\kit\\cache\nuv_python_dir = ..\\kit\\python\n"))
    monkeypatch.setattr(deps, "RUNTIME", tmp_path / "no-runtime")
    monkeypatch.setattr(deps.shutil, "which", lambda name: r"C:\Tools\uv.exe")
    for k in UV_VARS:
        monkeypatch.delenv(k, raising=False)
    assert deps.uv() == str(kit / "uv" / "uv.exe")  # the drive's before one on PATH: the exe's own choice
    env = deps.uv_env()
    assert env["UV_PYTHON_INSTALL_DIR"] == str(kit / "python")
    assert env["UV_CACHE_DIR"] == str(kit / "cache") and env["UV_PYTHON_PREFERENCE"] == "only-managed"
    monkeypatch.setenv("UV_CACHE_DIR", r"X:\own")  # the environment already says where (the drive's own scripts)
    assert deps.uv_env()["UV_CACHE_DIR"] == r"X:\own"
    monkeypatch.setattr(deps, "DRIVE", _settings(home, "uv = ..\\kit\\gone\\uv.exe\n"))  # named, but not there
    assert deps.uv() == r"C:\Tools\uv.exe"
    monkeypatch.setattr(deps, "DRIVE", NO_SETTINGS)
    assert deps.uv() == r"C:\Tools\uv.exe" and "UV_PYTHON_PREFERENCE" not in deps.uv_env()
    monkeypatch.setattr(deps.shutil, "which", lambda name: None)
    assert deps.uv() is None


def test_a_release_keeps_uv_and_python_in_its_runtime(monkeypatch, tmp_path):
    """No drive settings: IndiePulse.exe put uv into runtime\\uv and Python into runtime\\python — everything stays
    there (a portable copy leaves nothing in the profile, an uninstall takes it all), whatever the user's environment
    says."""
    runtime = tmp_path / "IndiePulse" / "runtime"
    (runtime / "uv").mkdir(parents=True)
    (runtime / "uv" / "uv.exe").write_bytes(b"")
    monkeypatch.setattr(deps, "DRIVE", NO_SETTINGS)
    monkeypatch.setattr(deps, "RUNTIME", runtime)
    monkeypatch.setattr(deps.shutil, "which", lambda name: r"C:\Tools\uv.exe")
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", r"C:\Users\me\uv-pythons")
    assert deps.uv() == str(runtime / "uv" / "uv.exe")
    env = deps.uv_env()
    assert env["UV_PYTHON_INSTALL_DIR"] == str(runtime / "python") and env["UV_CACHE_DIR"] == str(deps.uv_cache())
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
    # the cache: in runtime\, unless that is too deep for a package built from source (then a short one in %TEMP%)
    shallow = Path("C:/IndiePulse/runtime")
    monkeypatch.setattr(deps, "RUNTIME", shallow)
    assert deps.uv_cache() == shallow / "cache"
    deep = Path("C:/" + "/".join(["a-rather-long-folder-name"] * 4)) / "IndiePulse" / "runtime"
    monkeypatch.setattr(deps, "RUNTIME", deep)
    assert deps.uv_cache().parent == Path(deps.tempfile.gettempdir()).resolve()
    assert deps.uv_cache().name.startswith("IndiePulse-uv-")


def test_a_short_cache_in_temp_goes_once_used(monkeypatch, tmp_path):
    """A portable copy in a deep folder leaves nothing in %TEMP% after a repair or an install."""
    runtime = tmp_path.joinpath(*["a-rather-long-folder-name"] * 3) / "IndiePulse" / "runtime"
    (runtime / "uv").mkdir(parents=True)
    (runtime / "uv" / "uv.exe").write_bytes(b"")
    temp = tmp_path / "temp"
    monkeypatch.setattr(deps, "DRIVE", NO_SETTINGS)
    monkeypatch.setattr(deps, "RUNTIME", runtime)
    monkeypatch.setattr(deps.tempfile, "gettempdir", lambda: str(temp))
    monkeypatch.setattr(deps, "_STAMP", tmp_path / "stamp")
    cache = deps.uv_cache()
    assert cache.parent == temp.resolve()

    def fake_call(cmd, log, env=None):
        assert env["UV_CACHE_DIR"] == str(cache)
        cache.mkdir(parents=True, exist_ok=True)  # what uv does with it
        return True
    monkeypatch.setattr(deps, "_call", fake_call)
    assert deps.install_requirements(tmp_path / "pip.log") and not cache.exists()
    assert deps._repair_python(tmp_path / "venv.log") and not cache.exists()
    monkeypatch.setattr(deps, "RUNTIME", tmp_path / "IndiePulse" / "runtime")  # a shallow one keeps its own cache
    own = deps.uv_cache()
    own.mkdir(parents=True)
    deps._drop_temp_cache()
    assert own.exists()


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
    monkeypatch.setattr(deps, "uv", lambda: r"F:\Tools\uv\uv.exe")
    deps.install_requirements(tmp_path / "pip.log")
    assert calls[-1][:3] == [r"F:\Tools\uv\uv.exe", "pip", "install"] and calls[-1][-1] == str(deps.LOCK)
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
    monkeypatch.setattr(winapi, "DRIVE", NO_SETTINGS)
    monkeypatch.setattr(winapi, "PORTABLE_MARKER", tmp_path / "portable.txt")
    monkeypatch.setattr(winapi, "_machine_id", lambda: "11111111-AAAA")
    assert winapi.trusted_pc()  # no drive settings: an installed program
    (tmp_path / "portable.txt").write_text("")
    assert not winapi.trusted_pc()  # a portable copy: its setup promised to leave nothing on the computer
    home = tmp_path / "IndiePulse"
    monkeypatch.setattr(winapi, "DRIVE", _settings(home, "trusted_pcs = trusted-pcs.txt\n"))
    assert not winapi.trusted_pc()  # a list of trusted computers, but not there yet
    listed = home / "trusted-pcs.txt"
    listed.write_text("\ufeffCLUB-PC-07|22222222-bbbb\n", encoding="utf-8")  # PowerShell's UTF-8 has a BOM
    assert not winapi.trusted_pc()
    listed.write_text("\ufeffCLUB-PC-07|22222222-bbbb\nHOME-PC|11111111-aaaa\n", encoding="utf-8")
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
    monkeypatch.setattr(paths, "DRIVE", NO_SETTINGS)
    assert paths._data_dir() == tmp_path / "AppData" / "IndiePulse"  # an installed program
    monkeypatch.setattr(paths, "DRIVE", _settings(home, "portable = yes\n"))  # the drive says portable
    assert paths._data_dir() == home / "data"
    monkeypatch.setattr(paths, "DRIVE", NO_SETTINGS)
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


def test_the_drive_settings_file(monkeypatch, tmp_path):
    """IndiePulse.portable.ini: next to IndiePulse.exe before the one in the root of its drive; comments, a BOM, keys
    in any case, quotes; unknown keys and odd lines left out (and kept for the log); the path rules. tmp stands for
    the drive's root."""
    monkeypatch.setattr(drive_settings, "_drive_root", lambda p: tmp_path)
    home = tmp_path / "IndiePulse"
    home.mkdir()
    s = drive_settings.load(home)
    assert s.file is None and not s.portable and s.uv is s.uv_cache_dir is s.uv_python_dir is s.trusted_pcs is None
    (tmp_path / drive_settings.NAME).write_text("portable = yes\n", encoding="utf-8")
    s = drive_settings.load(home)
    assert s.file == tmp_path / drive_settings.NAME and s.portable
    monkeypatch.setenv("IP_TEST_TOOLS", r"Q:\tools")
    (home / drive_settings.NAME).write_text(
        "\ufeff# next to the exe: this one counts, the root's isn't read\n"
        "; a comment\n"
        "\n"
        "  PORTABLE = No\n"
        "Uv = \\kit\\uv.exe\n"                         # from the root of the ini's drive
        "uv_cache_dir = cache\n"                       # from the ini's folder
        "uv_python_dir = ../python\n"
        'trusted_pcs = "%IP_TEST_TOOLS%\\trusted pcs.txt"\n'
        "colour = blue\n"
        "[IndiePulse]\n", encoding="utf-8")
    s = drive_settings.load(home)
    assert s.file == home / drive_settings.NAME and s.portable is False
    assert s.uv == tmp_path / "kit" / "uv.exe" and s.uv_cache_dir == home / "cache"
    assert s.uv_python_dir == tmp_path / "python" and s.trusted_pcs == Path(r"Q:\tools\trusted pcs.txt")
    assert len(s.problems) == 2 and "'colour'" in s.problems[0] and "[IndiePulse]" in s.problems[1]
    (home / drive_settings.NAME).write_text(
        "uv = D:\\uv\\uv.exe\nuv =\nportable = maybe\nuv_cache_dir = a|b\nuv_python_dir = \\\\server\\share\\py\n",
        encoding="utf-8")
    s = drive_settings.load(home)
    assert s.uv is None and not s.portable and s.uv_cache_dir is None and len(s.problems) == 2  # the last line counts
    assert s.uv_python_dir == Path(r"\\server\share\py")
    assert drive_settings._path("D:\\uv\\uv.exe", home / drive_settings.NAME) == Path(r"D:\uv\uv.exe")  # as written
    monkeypatch.undo()
    assert drive_settings._drive_root(Path(r"F:\Games\IndiePulse")) == Path("F:\\")


def test_a_kit_on_the_drive(monkeypatch, tmp_path):
    """A drive that carries IndiePulse (a release in IndiePulse\\) and a toolkit for other people's computers, with
    KIT_INI in its root: each decision is the one IndiePulse made when it looked for such a kit in the root of its
    drive itself. tmp stands for the drive."""
    drive = tmp_path / "drive"
    home, kit = drive / "IndiePulse", drive / "Toolkit"
    (home / "lib").mkdir(parents=True)
    (kit / "tools" / "uv").mkdir(parents=True)
    (kit / "tools" / "uv" / "uv.exe").write_bytes(b"")
    (kit / "data").mkdir()
    (drive / drive_settings.NAME).write_text(KIT_INI, encoding="utf-8")
    real_root = drive_settings._drive_root
    monkeypatch.setattr(drive_settings, "_drive_root", lambda p: drive if drive in (p, *p.parents) else real_root(p))
    s = drive_settings.load(home)
    assert s.file == drive / drive_settings.NAME and s.problems == []

    # portable, without a portable.txt: data\ next to IndiePulse.exe (and IndiePulse.exe's setup preselects Portable)
    monkeypatch.setattr(paths, "ROOT", home / "lib")
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "DRIVE", s)
    monkeypatch.delenv("INDIEPULSE_DATA", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData"))
    assert s.portable and paths._data_dir() == home / "data"

    # the kit's uv, before the runtime's own and one on PATH
    monkeypatch.setattr(deps, "DRIVE", s)
    monkeypatch.setattr(deps, "RUNTIME", home / "runtime")
    (home / "runtime" / "uv").mkdir(parents=True)
    (home / "runtime" / "uv" / "uv.exe").write_bytes(b"")
    monkeypatch.setattr(deps.shutil, "which", lambda name: r"C:\Tools\uv.exe")
    assert deps.uv() == str(kit / "tools" / "uv" / "uv.exe")

    # uv's cache and Pythons in the kit, unless the environment already says where (the kit's own scripts may)
    for k in UV_VARS:
        monkeypatch.delenv(k, raising=False)
    env = deps.uv_env()
    assert env["UV_CACHE_DIR"] == str(kit / "data" / "uv" / "cache")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(kit / "data" / "uv" / "python")
    assert env["UV_PYTHON_PREFERENCE"] == "only-managed"
    monkeypatch.setenv("UV_CACHE_DIR", r"X:\own")
    monkeypatch.setenv("UV_PYTHON_PREFERENCE", "system")
    env = deps.uv_env()
    assert env["UV_CACHE_DIR"] == r"X:\own" and env["UV_PYTHON_PREFERENCE"] == "system"
    assert env["UV_PYTHON_INSTALL_DIR"] == str(kit / "data" / "uv" / "python")
    # a short cache in %TEMP% isn't used then, so it isn't IndiePulse's to remove
    monkeypatch.setattr(deps.tempfile, "gettempdir", lambda: str(tmp_path / "temp"))
    monkeypatch.setattr(deps, "RUNTIME", tmp_path.joinpath(*["a-rather-long-folder-name"] * 3) / "runtime")
    deps.RUNTIME.mkdir(parents=True)
    deps.uv_cache().mkdir(parents=True)
    deps._drop_temp_cache()
    assert deps.uv_cache().exists()

    # shortcuts and the autostart item only on a computer of the kit's list, whatever portable.txt says
    monkeypatch.setattr(winapi, "DRIVE", s)
    monkeypatch.setattr(winapi, "PORTABLE_MARKER", home / "portable.txt")
    monkeypatch.setattr(winapi, "_machine_id", lambda: "11111111-AAAA")
    assert not winapi.trusted_pc()  # no list yet
    listed = kit / "data" / "trusted-pcs.txt"
    listed.write_text("\ufeffCLUB-PC-07|22222222-bbbb\n", encoding="utf-8")
    assert not winapi.trusted_pc()
    listed.write_text("\ufeffCLUB-PC-07|22222222-bbbb\nHOME-PC|11111111-aaaa\n", encoding="utf-8")
    assert winapi.trusted_pc()
    (home / "portable.txt").write_text("", encoding="utf-8")
    assert winapi.trusted_pc()

    # and there, the first run of a release makes the shortcuts, as for a checkout
    monkeypatch.setattr(tray, "DRIVE", s)
    monkeypatch.setattr(tray, "is_release_layout", lambda: True)
    monkeypatch.setattr(tray, "_SHORTCUTS_MARKER", tmp_path / "shortcuts")
    assert tray._first_run_shortcuts(trusted=True) and not tray._first_run_shortcuts(trusted=False)
    (tmp_path / "shortcuts").write_text("", encoding="utf-8")
    assert not tray._first_run_shortcuts(trusted=True)  # made already
    (tmp_path / "shortcuts").unlink()
    monkeypatch.setattr(tray, "DRIVE", NO_SETTINGS)  # no list: a release's own setup made them, or promised not to
    assert not tray._first_run_shortcuts(trusted=True)
    monkeypatch.setattr(tray, "is_release_layout", lambda: False)
    assert tray._first_run_shortcuts(trusted=True)
