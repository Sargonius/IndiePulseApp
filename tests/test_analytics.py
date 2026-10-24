"""Analytics table: one row per publication (Brief) with its networks summed, video thumbnails."""
import json
import os
import re

from app.models.models import Brief, Post, PostStatusEnum
from tests.conftest import reload


def _publication(session, make_post, metrics_by_platform, **brief_kw):
    """A Brief published to several networks, each post with the given metrics."""
    first, *rest = metrics_by_platform.items()
    pid, _ = make_post(first[0], status=PostStatusEnum.done, **brief_kw)
    brief_id = reload(Post, pid).brief_id
    ids = [pid]
    for platform, _m in rest:
        p = Post(user_id=1, brief_id=brief_id, platform=platform, status=PostStatusEnum.done)
        session.add(p)
        session.commit()
        ids.append(p.id)
    for post_id, (_platform, m) in zip(ids, metrics_by_platform.items()):
        p = session.get(Post, post_id)
        p.metrics_json = json.dumps(m) if m is not None else None
        p.platform_url = f"https://example.com/{post_id}"
        session.add(p)
    session.commit()
    return brief_id, ids


def _group_html(html, brief_id):
    m = re.search(rf'<tbody class="pub-group" data-brief="{brief_id}">(.*?)</tbody>', html, re.S)
    assert m, f"no group for brief {brief_id}"
    return m.group(1)


def _sum_cell(group, key):
    m = re.search(rf'data-sum="{key}">(.*?)</td>', group, re.S)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


def test_posts_are_grouped_per_publication_with_summed_metrics(client, session, make_post):
    brief_id, ids = _publication(session, make_post, {
        "yt": {"likes": 14, "comments": 6, "views": 1125},
        "x": {"like": 14, "retweet": 2, "reply": 0, "impressions": 671},
        "discord": {"like": 0},
        "tg": None,                       # no metrics at all
    })
    html = client.get("/api/analytics/table").text
    group = _group_html(html, brief_id)
    assert group.count('class="analytics-row analytics-child') == 4
    assert all(f'data-post="{i}"' in group for i in ids)
    assert _sum_cell(group, "likes") == "28"
    assert _sum_cell(group, "reposts") == "2"
    assert _sum_cell(group, "comments") == "6"
    assert _sum_cell(group, "views") == "1796"
    assert _sum_cell(group, "total") == "🔥 36"
    assert "✓ 4" in group
    # best performer first inside the publication
    order = re.findall(r'analytics-child[^>]*data-post="(\d+)"', group)
    assert order[0] == str(ids[0]) and order[-1] in (str(ids[2]), str(ids[3]))


def test_publication_without_metrics_shows_dashes(client, session, make_post):
    brief_id, _ = _publication(session, make_post, {"vk": None, "tg": None})
    group = _group_html(client.get("/api/analytics/table").text, brief_id)
    assert _sum_cell(group, "likes") == "—"
    assert _sum_cell(group, "total") == "—"


def test_video_post_gets_a_poster_not_the_mp4(client, session, make_post):
    video_brief, _ = _publication(session, make_post, {"vk": None},
                                  media_path="media/zz_test_clip_nonexistent.mp4")
    image_brief, _ = _publication(session, make_post, {"vk": None},
                                  media_path="media/zz_test_shot.png")
    html = client.get("/api/analytics/table").text
    video = _group_html(html, video_brief)
    assert f'src="/api/media/poster/{video_brief}"' in video
    assert ".mp4" not in video and "is-video" in video
    assert 'src="/media/zz_test_shot.png"' in _group_html(html, image_brief)


def test_poster_endpoint_grabs_the_frame_once(client, session, make_post, tmp_path, monkeypatch):
    from app.services import thumbs, video_editor
    monkeypatch.setattr(thumbs, "MEDIA_DIR", str(tmp_path))
    (tmp_path / "clip.mp4").write_bytes(b"fake video")
    calls = []

    def fake_frame(src, out, at_sec=1.0):
        calls.append(src)
        with open(out, "wb") as f:
            f.write(b"\xff\xd8jpeg")
        return out
    monkeypatch.setattr(video_editor, "extract_poster_frame", fake_frame)
    brief_id, _ = _publication(session, make_post, {"x": None}, media_path="media/clip.mp4")

    for _ in range(2):
        r = client.get(f"/api/media/poster/{brief_id}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
        assert r.content == b"\xff\xd8jpeg"
    assert len(calls) == 1
    assert os.path.exists(tmp_path / "clip_poster.jpg")
    # once the poster exists the table links it directly
    assert thumbs.thumb_url(reload(Brief, brief_id)) == "/media/clip_poster.jpg"


def test_poster_endpoint_placeholder_when_video_is_gone(client, session, make_post):
    brief_id, _ = _publication(session, make_post, {"x": None}, media_path="media/zz_missing_clip.mp4")
    r = client.get(f"/api/media/poster/{brief_id}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")


def test_refresh_publication_refreshes_each_of_its_networks(client, session, make_post, monkeypatch):
    from app.jobs import tasks
    brief_id, ids = _publication(session, make_post, {"vk": None, "x": None, "tg": None})
    other_id, _ = make_post("vk", status=PostStatusEnum.done)
    refreshed = []
    monkeypatch.setattr(tasks.refresh_metrics_for_post, "call_local", lambda pid: refreshed.append(pid))
    r = client.post("/api/analytics/refresh_brief", data={"id": brief_id})
    assert r.status_code == 200 and 'class="pub-group"' in r.text
    assert sorted(refreshed) == sorted(ids) and other_id not in refreshed
