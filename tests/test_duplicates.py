"""The same publication never goes to a network twice (2026-09-30: a restart-triggered
reload re-submitted the Create Post form and every network got the post again)."""
import json
import uuid
from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app import platforms
from app.db.session import engine
from app.jobs import tasks
from app.models.models import Brief, Job, Post, PostStatusEnum
from app.services import duplicates
from tests.conftest import reload


@pytest.fixture
def pub(make_post):
    """make_post with one text per test: posts made by it are the same publication."""
    text = f"Build notes {uuid.uuid4().hex[:8]}"

    def _make(platform, payload=None, **kw):
        kw.setdefault("text_ru", text)
        kw.setdefault("text_en", text)
        return make_post(platform, payload, **kw)
    return _make


@pytest.fixture
def sent(monkeypatch):
    """Telegram publisher stub: records which posts actually went out."""
    out = []
    monkeypatch.setitem(platforms.PUBLISHERS, "tg", lambda session, post, job: out.append(post.id))
    return out


def _set(post_id, **fields):
    with Session(engine) as s:
        p = s.get(Post, post_id)
        for k, v in fields.items():
            setattr(p, k, v)
        s.add(p)
        s.commit()


def test_second_copy_is_cancelled_not_sent(pub, sent):
    first, first_job = pub("tg")
    assert tasks._publish_news_post_impl(first_job, first) == "ok"
    second, second_job = pub("tg")  # same text, another brief: a re-submitted form
    assert tasks._publish_news_post_impl(second_job, second) == "duplicate"
    assert sent == [first]
    post = reload(Post, second)
    assert post.status == PostStatusEnum.canceled
    assert f"{duplicates.MARK} #{first}" in post.error_text
    tasks._maybe_retry_publish(second_job, second, "duplicate")
    assert reload(Post, second).status == PostStatusEnum.canceled  # no auto-retry


def test_double_submit_while_both_queued(pub, sent):
    a, a_job = pub("tg")
    b, b_job = pub("tg")
    assert tasks._publish_news_post_impl(a_job, a) == "ok"   # queued twins don't block each other
    assert tasks._publish_news_post_impl(b_job, b) == "duplicate"
    assert sent == [a]


@pytest.mark.parametrize("change", [
    {"platform": "vk"},                                   # another network
    {"text_en": "Something else entirely"},               # another text
    {"media_path": "media/1790745327_other.mp4"},         # another file
])
def test_different_publications_go_out(pub, sent, monkeypatch, change):
    monkeypatch.setitem(platforms.PUBLISHERS, "vk", lambda session, post, job: sent.append(post.id))
    first, first_job = pub("tg", media_path="media/1790745327_clip.mp4")
    tasks._publish_news_post_impl(first_job, first)
    kw = {"media_path": "media/1790745327_clip.mp4", **change}
    second, second_job = pub(kw.pop("platform", "tg"), **kw)
    assert tasks._publish_news_post_impl(second_job, second) == "ok"
    assert sent == [first, second]


def test_same_file_uploaded_again_is_still_a_duplicate(pub, sent):
    first, first_job = pub("tg", media_path="media/1790745327_clip.mp4")
    tasks._publish_news_post_impl(first_job, first)
    second, second_job = pub("tg", media_path="media/1790999999_clip.mp4")
    assert tasks._publish_news_post_impl(second_job, second) == "duplicate"


def test_old_publication_can_be_repeated(pub, sent):
    first, first_job = pub("tg")
    tasks._publish_news_post_impl(first_job, first)
    _set(first, created_at=datetime.utcnow() - duplicates.WINDOW - timedelta(hours=1))
    second, second_job = pub("tg")
    assert tasks._publish_news_post_impl(second_job, second) == "ok"


def test_allow_flag_lets_a_deliberate_repeat_through(pub, sent):
    first, first_job = pub("tg")
    tasks._publish_news_post_impl(first_job, first)
    second, second_job = pub("tg", {duplicates.ALLOW: True})
    assert tasks._publish_news_post_impl(second_job, second) == "ok"
    assert sent == [first, second]


def test_retry_in_jobs_publishes_a_stopped_duplicate(pub, sent, client):
    first, first_job = pub("tg")
    tasks._publish_news_post_impl(first_job, first)
    second, second_job = pub("tg")
    tasks._publish_news_post_impl(second_job, second)
    assert client.post("/api/jobs/retry", data={"id": second_job}).status_code == 200
    with Session(engine) as s:
        new_job = s.exec(select(Job).where(Job.id > second_job).order_by(Job.id.desc())).first()
    payload = json.loads(new_job.payload_json)
    assert payload[duplicates.ALLOW] is True and payload["post_id"] == second
    assert tasks._publish_news_post_impl(new_job.id, second) == "ok"
    assert sent == [first, second]


# --- the form ------------------------------------------------------------------------

@pytest.fixture
def form(client, monkeypatch, tmp_path):
    from app.services import draft_store
    monkeypatch.setattr(draft_store, "DRAFT_PATH", str(tmp_path / "draft.json"))
    text = f"New build {uuid.uuid4().hex[:8]}"
    data = {"text_ru": "Новый билд", "text_en": text, "platforms": ["tg", "vk"],
            "media_normal": "1790745327_clip.mp4"}

    def submit(**extra):
        return client.post("/create/post", data={**data, **extra}, follow_redirects=False)

    def briefs():
        with Session(engine) as s:
            return [b.id for b in s.exec(select(Brief).where(Brief.text_en == text)).all()]
    submit.briefs = briefs
    return submit


def test_form_redirects_so_a_reload_cannot_resubmit(form, client):
    r = form()
    assert r.status_code == 303
    brief_id = form.briefs()[-1]
    assert r.headers["location"] == f"/published/{brief_id}"
    page = client.get(r.headers["location"])
    assert page.status_code == 200 and "Created publish jobs: 2" in page.text
    assert "const fromPost = false" in page.text


def test_form_refuses_to_create_the_same_publication_twice(form, client):
    form()
    before = form.briefs()
    r = form()  # the reload / double click
    assert r.status_code == 303
    assert r.headers["location"] == f"/published/{before[-1]}?duplicate=1"
    assert form.briefs() == before  # nothing created, nothing queued
    assert "not sent again" in client.get(r.headers["location"]).text


def test_form_publish_again_checkbox(form):
    form()
    r = form(allow_duplicate="1")
    assert r.headers["location"].endswith(f"/published/{form.briefs()[-1]}")
    assert len(form.briefs()) == 2
    with Session(engine) as s:
        posts = s.exec(select(Post).where(Post.brief_id == form.briefs()[-1])).all()
        payloads = [json.loads(j.payload_json) for j in s.exec(select(Job)).all()
                    if json.loads(j.payload_json).get("post_id") in {p.id for p in posts}]
    assert payloads and all(p[duplicates.ALLOW] for p in payloads)
