"""Release tooling: tools/build_release.py (the zip, latest.json, notes) and the two workflows."""
import hashlib
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_release as br  # noqa: E402

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="no git")


def _git(repo, *args):
    subprocess.run(["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", *args], cwd=repo,
                   check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    """A small IndiePulse-shaped git repo at version 2.1.0, tagged v2.0.0 earlier."""
    r = tmp_path / "repo"
    files = {
        "app/__init__.py": '__version__ = "2.0.0"\n',
        "app/main.py": "app = None\n",
        "app/templates/base.html": "<html></html>\n",
        "IndiePulse.pyw": "print('hi')\n",
        "IndiePulse.bat": "@echo off\nrem LF only in git\n",
        "requirements.txt": "httpx\n",
        "requirements.lock": "httpx==0.28.1\n",
        "README.md": "# IndiePulse\n",
        "LICENSE": "MIT\n",
        ".env.example": "APP_PORT=8000\n",
        "tests/test_x.py": "def test(): pass\n",
        ".claude/settings.local.json": "{}\n",
        ".github/workflows/ci.yml": "name: CI\n",
        "tools/lint.py": "pass\n",
        "Design.txt": "notes\n",
        "vendor/ffmpeg/README.txt": "put ffmpeg.exe here\n",
    }
    for rel, text in files.items():
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_bytes(text.encode())
    _git(r, "init", "-q")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "First")
    _git(r, "tag", "v2.0.0")
    (r / "app/__init__.py").write_text('__version__ = "2.1.0"\n')
    (r / "app/__pycache__").mkdir()
    (r / "app/__pycache__/main.cpython-312.pyc").write_bytes(b"junk")  # untracked: never shipped
    (r / ".settings.json").write_text('{"tg_bot_token": "secret"}')     # data: never shipped
    _git(r, "commit", "-q", "-am", "Faster calendar")
    _git(r, "commit", "-q", "--allow-empty", "-m", "Fix the Shorts board")
    return r


def test_tag_must_match_version(repo):
    assert br.check_tag("v2.1.0", repo) == "2.1.0"
    with pytest.raises(br.BuildError, match="doesn't match"):
        br.check_tag("v2.0.9", repo)
    (repo / "app/__init__.py").write_text('__version__ = "2.2.0-dev"\n')
    with pytest.raises(br.BuildError, match="isn't a release version"):
        br.check_tag("v2.2.0-dev", repo)


def test_cli_check_fails_on_a_mismatched_tag(capsys):
    assert br.main(["--tag", "v0.0.1", "--check"]) == 1
    assert "doesn't match" in capsys.readouterr().out


@needs_git
def test_zip_has_what_runs_and_nothing_else(repo, tmp_path):
    _git(repo, "tag", "-a", "v2.1.0", "-m", "Calendar is faster\n\n- and the Shorts board is fixed")
    out = tmp_path / "dist"
    m = br.build("v2.1.0", out, repo="me/IndiePulse", notes=br.notes_for_tag("v2.1.0", repo), root=repo,
                 published_at="2026-10-24T12:00:00Z")
    with zipfile.ZipFile(out / "IndiePulse-2.1.0.zip") as z:
        names = set(z.namelist())
        bat = z.read("IndiePulse/IndiePulse.bat")
    assert names == {f"IndiePulse/{p}" for p in (
        "app/__init__.py", "app/main.py", "app/templates/base.html", "IndiePulse.pyw", "IndiePulse.bat",
        "requirements.txt", "requirements.lock", "README.md", "LICENSE", ".env.example")}
    assert bat == b"@echo off\r\nrem LF only in git\r\n"
    data = (out / "IndiePulse-2.1.0.zip").read_bytes()
    assert m == json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert m["version"] == "2.1.0" and m["tag"] == "v2.1.0" and m["name"] == "IndiePulse-2.1.0.zip"
    assert m["url"] == "https://github.com/me/IndiePulse/releases/download/v2.1.0/IndiePulse-2.1.0.zip"
    assert m["sha256"] == hashlib.sha256(data).hexdigest() and m["size"] == len(data)
    assert m["notes"] == "Calendar is faster\n\n- and the Shorts board is fixed"
    assert m["published_at"] == "2026-10-24T12:00:00Z"
    assert m["sha256"] in (out / "notes.md").read_text(encoding="utf-8")


@needs_git
def test_lightweight_tag_notes_are_the_commit_subjects(repo):
    _git(repo, "tag", "v2.1.0")
    assert br.notes_for_tag("v2.1.0", repo) == "- Fix the Shorts board\n- Faster calendar"


def _workflow(name):
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8"))
    return wf, wf.get("on") or wf.get(True)  # YAML 1.1 reads a bare `on` as true


def test_ci_workflow_runs_lint_and_tests_on_windows():
    wf, on = _workflow("ci.yml")
    assert on["push"]["branches"] == ["main"] and on["pull_request"]["branches"] == ["main"]
    job = wf["jobs"]["test"]
    runs = " | ".join(s.get("run", "") for s in job["steps"])
    assert job["runs-on"] == "windows-latest"
    assert "--python 3.12" in runs and "requirements-dev.txt" in runs
    assert "tools\\lint.py" in runs and "-m pytest" in runs


def test_release_workflow_checks_the_tag_then_publishes_both_assets():
    wf, on = _workflow("release.yml")
    assert on["push"]["tags"] == ["v*"]
    assert wf["permissions"] == {"contents": "write"}
    steps = wf["jobs"]["release"]["steps"]
    runs = [s.get("run", "") for s in steps]
    check = next(i for i, r in enumerate(runs) if "--check" in r)
    tests = next(i for i, r in enumerate(runs) if "-m pytest" in r)
    publish = next(i for i, r in enumerate(runs) if "gh release create" in r)
    assert check < tests < publish
    assert "IndiePulse-$env:VERSION.zip" in runs[publish] and "dist\\latest.json" in runs[publish]
    assert steps[publish]["env"]["GH_TOKEN"] == "${{ github.token }}"
