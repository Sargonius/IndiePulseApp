import json
from datetime import datetime, timedelta

from sqlmodel import select

from app.jobs import tasks
from app.models.models import Job, JobStatusEnum, Post, PostStatusEnum
from tests.conftest import reload


def test_find_publish_jobs_matches_exact_post_id(make_post, session):
    ids = [make_post("tg") for _ in range(12)]
    target_post, target_job = ids[0]
    # a post whose id starts with the target's digits must not match
    others = {j for p, j in ids[1:]}
    found = {j.id for j in tasks.find_publish_jobs(session, target_post)}
    assert found == {target_job}
    assert not (found & others)


def test_publish_payload_for_keeps_media(make_post, session):
    post_id, _ = make_post("tg", {"media_rel": "a.png", "gallery": ["a.png", "b.png"], "publish_attempt": 1})
    payload = tasks.publish_payload_for(session, post_id)
    assert payload == {"post_id": post_id, "media_rel": "a.png", "gallery": ["a.png", "b.png"]}


def _age_post(session, post_id, **fields):
    p = session.get(Post, post_id)
    for k, v in fields.items():
        setattr(p, k, v)
    session.add(p)
    session.commit()


def test_watchdog_flags_stuck_running_without_republishing(make_post, session, monkeypatch):
    scheduled = []
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda **kw: scheduled.append(kw))
    post_id, job_id = make_post("tg", status=PostStatusEnum.running)
    # updated_at is maintained by an ORM hook, so age it with raw SQL
    from sqlmodel import text
    old = (datetime.utcnow() - timedelta(hours=3)).isoformat(sep=" ")
    session.exec(text(f"UPDATE posts SET updated_at='{old}' WHERE id={post_id}"))
    session.commit()

    tasks.enqueue_due_posts.call_local()
    post = reload(Post, post_id)
    assert post.status == PostStatusEnum.error
    assert "stuck_running" in post.error_text
    assert scheduled == []


def test_lost_queued_job_is_reenqueued_with_payload(make_post, session, monkeypatch):
    scheduled = []
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda **kw: scheduled.append(kw))
    post_id, job_id = make_post("tg", {"media_rel": "pic.png"})
    j = session.get(Job, job_id)
    j.status = JobStatusEnum.queued
    session.add(j)
    session.commit()
    _age_post(session, post_id, scheduled_at=datetime.now() - timedelta(hours=1))

    tasks.enqueue_due_posts.call_local()
    mine = [kw for kw in scheduled if kw["args"][1] == post_id]
    assert len(mine) == 1
    new_job_id = mine[0]["args"][0]
    assert new_job_id != job_id
    assert json.loads(reload(Job, new_job_id).payload_json)["media_rel"] == "pic.png"
    assert reload(Job, job_id).status == JobStatusEnum.error


def test_recently_due_post_with_queued_job_is_left_alone(make_post, session, monkeypatch):
    scheduled = []
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda **kw: scheduled.append(kw))
    post_id, job_id = make_post("tg")
    j = session.get(Job, job_id)
    j.status = JobStatusEnum.queued
    session.add(j)
    session.commit()
    _age_post(session, post_id, scheduled_at=datetime.now() - timedelta(minutes=2))
    tasks.enqueue_due_posts.call_local()
    assert post_id not in [kw["args"][1] for kw in scheduled]


def test_failed_metrics_fetch_keeps_previous_numbers(make_post, session, monkeypatch):
    post_id, _ = make_post("bluesky", status=PostStatusEnum.done)
    _age_post(session, post_id, platform_post_id="at://x", metrics_json=json.dumps({"like": 5, "reposts": 2}))
    from app.services import bluesky_client
    monkeypatch.setattr(bluesky_client.BlueskyClient, "get_post_metrics", staticmethod(lambda uri: {}))
    tasks.refresh_metrics_for_post.call_local(post_id)
    m = json.loads(reload(Post, post_id).metrics_json)
    assert m["like"] == 5 and m["reposts"] == 2
    assert m["error"] == "bluesky_fetch_failed" and m["stale"] is True


def test_updated_at_and_error_scrubbing(make_post, session, set_settings):
    set_settings(tg_bot_token="123456:SECRET-token-value-abcdef")
    post_id, _ = make_post("tg")
    p = session.get(Post, post_id)
    created = p.updated_at
    p.error_text = "failed: https://api.telegram.org/bot123456:SECRET-token-value-abcdef/sendMessage"
    session.add(p)
    session.commit()
    p = reload(Post, post_id)
    assert "SECRET" not in p.error_text
    assert p.updated_at >= created
