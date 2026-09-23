"""Test isolation: a throwaway SQLite DB, Huey queue and settings file per session.

Env vars must be set before any `app.*` import (config reads them at import time).
The working directory stays at the project root so templates/static resolve.
"""
import json
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TMP = tempfile.mkdtemp(prefix="indiepulse-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(_TMP, "test.db").replace("\\", "/")
os.environ["QUEUE_DB"] = os.path.join(_TMP, "queue.db")
os.environ.setdefault("APP_HOST", "127.0.0.1")
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from app.db.session import create_db_and_tables, engine  # noqa: E402
import app.models.models  # noqa: E402,F401  (registers tables before create_all)
from app.services import settings as settings_mod  # noqa: E402

create_db_and_tables()


@pytest.fixture(autouse=True)
def settings_file(tmp_path, monkeypatch):
    """Every test gets its own empty .settings.json (never the real one)."""
    path = tmp_path / "settings.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(settings_mod, "SETTINGS_PATH", str(path))
    from app.services import health_check
    monkeypatch.setattr(health_check, "LAST_RESULT_PATH", str(tmp_path / "health_last.json"))
    return path


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


@pytest.fixture
def make_post(session):
    """Create a Brief + Post (+ publish Job) and return (post_id, job_id)."""
    import json as _json
    from app.models.models import Brief, Post, Job, JobKindEnum, PostStatusEnum

    def _make(platform, payload=None, status=PostStatusEnum.queued, **brief_kw):
        brief = Brief(user_id=1, type="post", text_ru=brief_kw.pop("text_ru", "Привет"),
                      text_en=brief_kw.pop("text_en", "Hello"), **brief_kw)
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


def reload(model, pk):
    from sqlmodel import Session
    with Session(engine) as s:
        return s.get(model, pk)
