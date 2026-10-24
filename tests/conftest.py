"""Test isolation: a throwaway SQLite DB, Huey queue and settings file per session.

Env vars must be set before any `app.*` import (config reads them at import time).
The working directory is a throwaway data folder (INDIEPULSE_DATA, app.core.paths), never the project root: the
app opens its data by relative paths, and run from the owner's checkout — where the real .settings.json, DBs, logs
and the live app's .locks sit — anything not yet redirected would land there. Before this, a module-scoped
TestClient started the app (its startup migration saves the settings) before the per-test settings file was
patched in, and wrote the real .settings.json (2026-10-24). Templates and static are found from the code folder.
"""
import itertools
import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="indiepulse-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/")
os.environ["QUEUE_DB"] = os.path.join(_TMP, "queue.db")
os.environ["INDIEPULSE_DATA"] = _TMP
os.environ.setdefault("APP_HOST", "127.0.0.1")
os.chdir(_TMP)
sys.path.insert(0, ROOT)
# the video tests need ffmpeg: the one in the checkout's vendor/ (the app looks there relative to its data folder)
if os.path.isfile(os.path.join(ROOT, "vendor", "ffmpeg", "ffmpeg.exe")):
    os.environ["PATH"] = os.path.join(ROOT, "vendor", "ffmpeg") + os.pathsep + os.environ.get("PATH", "")

from app.db.session import create_db_and_tables, engine  # noqa: E402
import app.models.models  # noqa: E402,F401  (registers tables before create_all)
from app.services import settings as settings_mod  # noqa: E402

create_db_and_tables()


# Setup done: pages render as on an install that is set up (a fresh one's home page is the setup wizard, see
# tests/test_setup.py, which starts from FRESH_SETTINGS).
SET_UP_SETTINGS = '{"setup_state": {"done": true, "step": "done", "fresh": false, "at": 0}}'
FRESH_SETTINGS = "{}"


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """Every test gets its own .settings.json (never the real one), empty but for the setup wizard marked done."""
    path = tmp_path / "settings.json"
    path.write_text(SET_UP_SETTINGS, encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", str(path))
    from app.services import health_check
    monkeypatch.setattr(health_check, "LAST_RESULT_PATH", str(tmp_path / "health_last.json"))
    # the post forms clear the draft after queueing: never the real one
    from app.services import draft_store
    monkeypatch.setattr(draft_store, "DRAFT_PATH", str(tmp_path / "draft.json"))
    return path


from app.services import marketing_collect as _marketing_collect  # noqa: E402

REAL_SNAPSHOT_BEFORE_PUBLISH = _marketing_collect.snapshot_before_publish


@pytest.fixture(autouse=True)
def no_follower_snapshot(monkeypatch):
    """Publishing reads follower counts first (network); tests use REAL_SNAPSHOT_BEFORE_PUBLISH."""
    monkeypatch.setattr(_marketing_collect, "snapshot_before_publish", lambda *a, **kw: "off")


@pytest.fixture
def set_settings(settings_file):
    def _set(**values):
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        data.update(values)
        settings_file.write_text(json.dumps(data), encoding="utf-8")
    return _set


@pytest.fixture
def session():
    from sqlmodel import Session
    with Session(engine) as s:
        yield s


_brief_seq = itertools.count(1)


@pytest.fixture
def make_post(session):
    """Create a Brief + Post (+ publish Job) and return (post_id, job_id)."""
    import json as _json
    from app.models.models import Brief, Post, Job, JobKindEnum, PostStatusEnum

    def _make(platform, payload=None, status=PostStatusEnum.queued, **brief_kw):
        # distinct default texts: the duplicate guard would otherwise treat the posts of
        # different tests (one DB per session) as the same publication
        n = next(_brief_seq)
        brief = Brief(user_id=1, type="post", text_ru=brief_kw.pop("text_ru", f"Привет {n}"),
                      text_en=brief_kw.pop("text_en", f"Hello {n}"), **brief_kw)
        session.add(brief)
        session.commit()
        post = Post(user_id=1, brief_id=brief.id, platform=platform, status=status)
        session.add(post)
        session.commit()
        job = Job(kind=JobKindEnum.publish,
                  payload_json=_json.dumps({"post_id": post.id, **(payload or {})}))
        session.add(job)
        session.commit()
        return post.id, job.id
    return _make


@pytest.fixture
def project(session):
    def _make(name="Game", **kw):
        from app.models.models import Project
        p = Project(user_id=1, name=name, **kw)
        session.add(p)
        session.commit()
        session.refresh(p)
        return p
    return _make


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def reload(model, pk):
    from sqlmodel import Session
    with Session(engine) as s:
        return s.get(model, pk)
