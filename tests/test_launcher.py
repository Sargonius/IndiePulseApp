"""IndiePulse.exe (launcher/, C#) and the Python side agree about a folder: where its code, runtime, venv and data are,
what the requirements stamp says, what the folder's named events are called, what still has to be prepared, what its
drive settings (IndiePulse.portable.ini) say. The exe is built here with the C# compiler that is part of Windows
(tools/build_launcher.ps1) and asked with --paths, on folders made up in tmp; skipped where there is no such
compiler."""
import ctypes
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from app.core import drive_settings, paths
from app.desktop import apply_update, deps
from tests.test_portable import KIT_INI, UV_VARS

ROOT = Path(__file__).resolve().parents[1]
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not CSC.is_file(), reason="needs the C# compiler of Windows")


@pytest.fixture(scope="module")
def exe(tmp_path_factory):
    out = tmp_path_factory.mktemp("launcher") / "IndiePulse.exe"
    r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(ROOT / "tools" / "build_launcher.ps1"), "-Out", str(out)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout + r.stderr
    return out


def _code(root: Path, version: str) -> None:
    (root / "app").mkdir(parents=True)
    (root / "app" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    (root / "requirements.txt").write_text("httpx~=0.28\n", encoding="utf-8")
    (root / "requirements.lock").write_text("httpx==0.28.1\nanyio==4.9.0\n", encoding="utf-8")


@pytest.fixture
def release(tmp_path, exe):
    """An unzipped release: IndiePulse.exe, README, the code in lib\\."""
    home = tmp_path / "IndiePulse"
    _code(home / "lib", "2.1.0")
    (home / "README.md").write_text("# IndiePulse\n", encoding="utf-8")
    shutil.copy(exe, home / "IndiePulse.exe")
    return home


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """The environment both sides see: no INDIEPULSE_DATA of the test run, no uv folders of this computer's, a
    LOCALAPPDATA of tmp."""
    for k in ("INDIEPULSE_DATA", "INDIEPULSE_INSTANCE", *UV_VARS):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    return tmp_path


def ask(home: Path, exe_path: Path | None = None) -> dict:
    out = home.parent / f"paths-{home.name}.json"
    r = subprocess.run([str(exe_path or home / "IndiePulse.exe"), "--paths", str(out)], timeout=60)
    assert r.returncode == 0
    return json.loads(out.read_text(encoding="utf-8"))


def _drive_json(s: drive_settings.DriveSettings) -> dict:
    """The drive settings as --paths writes them."""
    return {"file": str(s.file) if s.file else None, "portable": s.portable,
            **{k: str(getattr(s, k)) if getattr(s, k) else None for k in drive_settings.KEYS[1:]}}


def python_side(monkeypatch, home: Path, root: Path) -> dict:
    monkeypatch.setattr(paths, "ROOT", root)
    monkeypatch.setattr(paths, "HOME", home)
    monkeypatch.setattr(paths, "DRIVE", drive_settings.load(home))
    monkeypatch.setattr(deps, "DRIVE", paths.DRIVE)
    monkeypatch.setattr(deps, "REQUIREMENTS", root / "requirements.txt")
    monkeypatch.setattr(deps, "LOCK", root / "requirements.lock")
    return {"data": str(paths._data_dir()), "key": apply_update.instance_key(home),
            "requirements_hash": deps._requirements_hash(), "version": apply_update.version_of(home),
            "root": str(apply_update.code_dir(home)), "venv": str(apply_update.venv_python(home).parent.parent),
            "drive_settings": _drive_json(paths.DRIVE)}


def _drive_uv(home: Path) -> bool:
    """The drive settings of `home` name a uv that is there (the drive the tests run on may have them)."""
    uv = drive_settings.load(home).uv
    return bool(uv and uv.is_file())


def test_a_release_folder(release, clean_env, monkeypatch):
    j = ask(release)
    py = python_side(monkeypatch, release, release / "lib")
    assert j["home"] == str(release) and j["release"] is True
    assert j["root"] == py["root"] == str(release / "lib")
    assert j["runtime"] == str(release / "runtime") and j["venv"] == py["venv"] == str(release / "runtime" / "venv")
    assert j["data"] == py["data"] and j["drive_settings"] == py["drive_settings"]
    if drive_settings.find(release) is None:  # no drive settings where the tests run: installed, not portable
        assert j["data"] == str(clean_env / "local" / "IndiePulse") and j["portable"] is False
        assert j["drive_settings"]["file"] is None and j["uv_env"] == {
            "UV_CACHE_DIR": j["uv_cache"], "UV_PYTHON_INSTALL_DIR": str(release / "runtime" / "python"),
            "UV_PYTHON_PREFERENCE": "only-managed"}
    assert j["version"] == py["version"] == "2.1.0"
    assert j["requirements_hash"] == py["requirements_hash"]
    assert j["key"] == py["key"] and len(j["key"]) == 16
    assert j["install_dir"] == str(clean_env / "local" / "Programs" / "IndiePulse")
    assert j["installed_here"] is False and j["venv_alive"] is False
    assert j["plan"] == (["python", "venv", "packages"] if _drive_uv(release) else ["uv", "python", "venv", "packages"])


def test_portable_and_the_other_data_rules(release, clean_env, monkeypatch):
    (release / "portable.txt").write_text("", encoding="utf-8")
    j = ask(release)
    assert j["portable"] is True and j["data"] == str(release / "data")
    assert j["data"] == python_side(monkeypatch, release, release / "lib")["data"]
    (release / "lib" / ".settings.json").write_text("{}", encoding="utf-8")  # a copy that ran with its data there
    assert ask(release)["data"] == str(release / "lib") == python_side(monkeypatch, release, release / "lib")["data"]
    monkeypatch.setenv("INDIEPULSE_DATA", str(clean_env / "elsewhere"))
    assert ask(release)["data"] == str(clean_env / "elsewhere")


def test_a_checkout_is_its_own_code_folder(tmp_path, exe, clean_env, monkeypatch):
    home = tmp_path / "checkout"
    _code(home, "2.2.0-dev")
    shutil.copy(exe, home / "IndiePulse.exe")
    j = ask(home)
    py = python_side(monkeypatch, home, home)
    assert j["release"] is False and j["root"] == py["root"] == str(home)
    assert j["venv"] == py["venv"] == str(home / ".venv") and j["version"] == "2.2.0-dev"
    assert j["data"] == py["data"] and j["requirements_hash"] == py["requirements_hash"]


def test_the_venv_and_what_is_left_to_prepare(release, clean_env, monkeypatch):
    """deps.venv_alive() and the stamp, read by both: a venv whose base Python is here, and the stamp of these
    requirements, need nothing more."""
    base = clean_env / "python"
    base.mkdir()
    (base / "python.exe").write_bytes(b"MZ")
    venv = release / "runtime" / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "pythonw.exe").write_bytes(b"MZ")
    (venv / "Scripts" / "python.exe").write_bytes(b"MZ")
    (venv / "pyvenv.cfg").write_text(f"home = {base}\nversion_info = 3.12.15\n", encoding="utf-8")
    monkeypatch.setattr(deps, "VENV", venv)
    monkeypatch.setattr(deps, "_STAMP", venv / "indiepulse-requirements.sha256")
    j = ask(release)
    assert j["venv_alive"] is True and deps.venv_alive()
    no_uv = [] if _drive_uv(release) else ["uv"]
    assert j["plan"] == no_uv + ["packages"]          # no stamp yet; no uv and no pip to install with
    (release / "runtime" / "uv").mkdir()
    (release / "runtime" / "uv" / "uv.exe").write_bytes(b"MZ")
    assert ask(release)["plan"] == ["packages"]
    python_side(monkeypatch, release, release / "lib")
    (venv / "indiepulse-requirements.sha256").write_text(deps._requirements_hash(), encoding="ascii")
    assert ask(release)["plan"] == [] and not deps.requirements_changed()
    (base / "python.exe").unlink()                     # an antivirus took the base Python's python.exe
    j = ask(release)
    assert j["venv_alive"] is False and not deps.venv_alive() and j["plan"] == ["python", "venv", "packages"]


def test_a_deep_folder_gets_a_short_uv_cache(tmp_path, exe, release, clean_env, monkeypatch):
    """A package without a wheel (huey) is built ~130 characters down in uv's cache: under a deep folder that passes
    Windows' 260 (it did in a test run, 2026-10-25), so both sides then use a short cache in %TEMP%."""
    home = tmp_path.joinpath(*["a-rather-long-folder-name"] * 3) / "IndiePulse"
    _code(home / "lib", "2.1.0")
    shutil.copy(exe, home / "IndiePulse.exe")
    j = ask(home)
    monkeypatch.setattr(deps, "RUNTIME", home / "runtime")
    assert len(str(home / "runtime" / "cache")) > deps.DEEP_CACHE
    assert j["uv_cache"] == str(deps.uv_cache()) and Path(j["uv_cache"]).parent == Path(os.environ["TEMP"]).resolve()
    shallow = ask(release)
    monkeypatch.setattr(deps, "RUNTIME", release / "runtime")
    if len(str(release / "runtime" / "cache")) <= deps.DEEP_CACHE:
        assert shallow["uv_cache"] == str(deps.uv_cache()) == str(release / "runtime" / "cache")


def test_the_drive_settings_read_alike(release, clean_env, monkeypatch):
    """IndiePulse.portable.ini next to the exe: both sides read the same values with the same path rules (\\folder
    from the root of the ini's drive, others from its folder, %VARS%, quotes), and the same uv and uv environment come
    of them, the environment's own UV_CACHE_DIR still first."""
    tools = clean_env / "tools"
    (tools / "uv").mkdir(parents=True)
    (tools / "uv" / "uv.exe").write_bytes(b"MZ")
    monkeypatch.setenv("IP_TEST_TOOLS", str(tools))
    (release / drive_settings.NAME).write_text(
        "\ufeff# what the drive says\n"
        "Portable = Yes\n"
        "; uv from the tools beside IndiePulse\n"
        "uv = ..\\tools\\uv\\uv.exe\n"
        "UV_CACHE_DIR = \\uv-cache\n"
        "uv_python_dir = %IP_TEST_TOOLS%/python\n"
        'trusted_pcs = "%IP_TEST_TOOLS%\\trusted pcs.txt"\n'
        "colour = blue\n", encoding="utf-8")
    j = ask(release)
    py = python_side(monkeypatch, release, release / "lib")
    assert j["drive_settings"] == py["drive_settings"] == {
        "file": str(release / drive_settings.NAME), "portable": True, "uv": str(tools / "uv" / "uv.exe"),
        "uv_cache_dir": str(Path(release.anchor) / "uv-cache"), "uv_python_dir": str(tools / "python"),
        "trusted_pcs": str(tools / "trusted pcs.txt")}
    assert j["portable"] is True and j["data"] == py["data"] == str(release / "data")
    monkeypatch.setattr(deps, "RUNTIME", release / "runtime")
    assert j["uv"] == deps.uv() == str(tools / "uv" / "uv.exe") and j["plan"] == ["python", "venv", "packages"]
    env = deps.uv_env()
    assert j["uv_env"] == {k: env[k] for k in UV_VARS} == {
        "UV_CACHE_DIR": str(Path(release.anchor) / "uv-cache"), "UV_PYTHON_INSTALL_DIR": str(tools / "python"),
        "UV_PYTHON_PREFERENCE": "only-managed"}
    monkeypatch.setenv("UV_CACHE_DIR", str(clean_env / "own-cache"))
    assert ask(release)["uv_env"]["UV_CACHE_DIR"] == deps.uv_env()["UV_CACHE_DIR"] == str(clean_env / "own-cache")


def test_a_kit_on_the_drive_seen_by_the_exe(release, clean_env, monkeypatch):
    """KIT_INI (tests/test_portable.py) next to the exe: its \\Toolkit paths are on the exe's drive, whatever its
    letter, and the exe makes of them what the Python side does — portable, data\\ next to it, uv's folders."""
    (release / drive_settings.NAME).write_text(KIT_INI, encoding="utf-8")
    j = ask(release)
    py = python_side(monkeypatch, release, release / "lib")
    kit = Path(release.anchor) / "Toolkit"
    assert j["drive_settings"] == py["drive_settings"] == {
        "file": str(release / drive_settings.NAME), "portable": True, "uv": str(kit / "tools" / "uv" / "uv.exe"),
        "uv_cache_dir": str(kit / "data" / "uv" / "cache"), "uv_python_dir": str(kit / "data" / "uv" / "python"),
        "trusted_pcs": str(kit / "data" / "trusted-pcs.txt")}
    assert j["portable"] is True and j["data"] == py["data"] == str(release / "data")
    env = deps.uv_env()
    assert j["uv_env"] == {k: env[k] for k in UV_VARS} == {
        "UV_CACHE_DIR": str(kit / "data" / "uv" / "cache"),
        "UV_PYTHON_INSTALL_DIR": str(kit / "data" / "uv" / "python"), "UV_PYTHON_PREFERENCE": "only-managed"}


def test_the_same_key_from_a_short_path(release, clean_env):
    """%TEMP% is often spelt with 8.3 names (C:\\Users\\SARGON~1\\...): the exe's key is the long path's, as Python's."""
    buf = ctypes.create_unicode_buffer(1024)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(release / "IndiePulse.exe"), buf, 1024) or "~" not in buf.value:
        pytest.skip("no 8.3 names on this volume")
    j = ask(release, Path(buf.value))
    assert j["home"] == str(release) and j["key"] == apply_update.instance_key(release)


def test_a_sandboxed_profile_is_followed(release, clean_env, monkeypatch):
    """LOCALAPPDATA / APPDATA / USERPROFILE pointed elsewhere (a test, a sandbox): the setup's folders go there."""
    profile = clean_env / "profile"
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(profile / "AppData" / "Roaming"))
    j = ask(release)
    assert j["desktop"] == str(profile / "Desktop")
    assert j["start_menu"] == str(profile / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    assert j["install_dir"] == str(clean_env / "local" / "Programs" / "IndiePulse")
