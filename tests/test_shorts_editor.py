"""Shorts editor: overlay validation, text layout/rendering, the checklist linter,
its endpoints, and trim/overlays stored on the Brief by the factory submit."""
import io
import json
import os

import pytest
from PIL import Image

from app.services import shorts_lint as sl
from app.services import text_overlay as to

needs_fonts = pytest.mark.skipif(not to._candidates(False) or not to._candidates(True),
                                 reason="no system fonts")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app, base_url="http://127.0.0.1:8000") as c:
        yield c


def codes(findings, level=None):
    return {f["code"] for f in findings if level is None or f["level"] == level}


# ── validation ───────────────────────────────────────────────────────────────

def test_clean_overlays_drops_clamps_and_limits():
    items = [
        {"text": "  ", "start": 0, "end": 2},                          # empty → dropped
        {"text": "x" * 200, "start": -3, "end": 2, "pos": "bottom", "size": "xl"},
        {"text": "late", "start": 18, "end": 99},                      # end clamped to 20
        {"text": "backwards", "start": 5, "end": 4},                   # end ≤ start → dropped
        "junk",
    ] + [{"text": f"t{i}", "start": i, "end": i + 1} for i in range(10)]
    out = to.clean_overlays(items, clip_duration=20)
    assert len(out) == to.MAX_OVERLAYS
    first = next(o for o in out if o["text"].startswith("x"))
    assert len(first["text"]) == to.MAX_TEXT_LEN
    assert first["start"] == 0 and first["pos"] == "middle" and first["size"] == "m"
    late = next(o for o in out if o["text"] == "late")
    assert late["end"] == 20
    assert all(o["text"] not in ("", "backwards") for o in out)
    assert [o["start"] for o in out] == sorted(o["start"] for o in out)
    assert to.clean_overlays("nope") == []


def test_clean_trim():
    assert to.clean_trim(5, 25) == (5.0, 25.0)
    assert to.clean_trim(0, None) == (None, None)
    assert to.clean_trim("abc", -1) == (None, None)
    assert to.clean_trim(10, 10.2) == (10.0, None)  # too short → end ignored


# ── scripts, fonts, wrapping ─────────────────────────────────────────────────

def test_detect_script():
    assert to.detect_script("Wishlist on Steam") == "lat"
    assert to.detect_script("Добавь в вишлист") == "cyr"
    assert to.detect_script("ゲーム制作 Steam") == "ja"
    assert to.detect_script("— 123 !") is None


@needs_fonts
def test_font_selection_by_script():
    lat, cyr, ja = to.find_font("Wishlist"), to.find_font("Привет"), to.find_font("夜のドライブ")
    assert os.path.basename(lat).lower() in [f.lower() for f in to.LATIN_FONTS] or "fonts" in lat
    assert to.font_covers(cyr, "Привет ЖЯ")
    assert to.font_covers(ja, "夜のドライブ")
    assert not to.has_cjk("Привет") and to.has_cjk("夜")


def test_find_font_fails_clearly(monkeypatch):
    monkeypatch.setattr(to, "_candidates", lambda cjk: [])
    with pytest.raises(RuntimeError, match="font"):
        to.find_font("Hello")


@needs_fonts
def test_wrap_latin_and_cyrillic_by_words():
    for text in ("I rebuilt the whole combat system after one angry review",
                 "Игрок написал что бой отстой и я всё переделал за неделю"):
        lay = to.layout_text(text, "l")
        assert lay["fits"] and len(lay["lines"]) == 2
        assert " ".join(lay["lines"]) == text  # broken only at spaces
        font = to._load_font(lay["font_path"], lay["font_size"])
        assert all(to._text_w(font, ln, 0) <= to.MAX_TEXT_W for ln in lay["lines"])


@needs_fonts
def test_wrap_japanese_by_character_with_kinsoku():
    text = "新しいエリアのネオンが映える街並みを作っています。"
    font = to._load_font(to.find_font(text), 108)
    lines = to.wrap_text(text, font, to.MAX_TEXT_W)
    assert len(lines) >= 2 and "".join(lines) == text
    assert not any(ln[0] in "。、" for ln in lines)


@needs_fonts
def test_wrap_never_leaves_an_opening_bracket_at_a_line_end():
    text = "輸送隊強盗、初挑戦（失敗）"   # wrapped once with "（" stranded on line 1
    font = to._load_font(to.find_font(text), 108)
    for width in range(500, 1000, 20):
        lines = to.wrap_text(text, font, width)
        assert "".join(lines) == text
        assert not any(len(ln) > 1 and ln[-1] in "（「『" for ln in lines), (width, lines)


@needs_fonts
def test_short_text_keeps_size_and_overflow_shrinks_then_truncates():
    short = to.layout_text("Hook", "l")
    assert short["font_size"] == to.SIZES["l"] and not short["shrunk"]
    mid = to.layout_text("This line is long enough that it will need a smaller font here", "l")
    assert mid["shrunk"] and mid["fits"]
    huge = to.layout_text("word " * 24, "l")
    assert huge["font_size"] >= to.MIN_FONT and not huge["fits"]
    assert len(huge["lines"]) <= to.HARD_MAX_LINES


@needs_fonts
@pytest.mark.parametrize("pos", list(to.POSITIONS))
def test_png_alpha_in_expected_band_and_safe_zone(pos):
    img = Image.open(io.BytesIO(to.render_overlay_png("Wishlist on Steam today", pos, "l")))
    assert img.size == (1080, 1920) and img.mode == "RGBA"
    bbox = img.getchannel("A").getbbox()
    assert bbox is not None
    _l, top, _r, bottom = bbox
    assert top >= to.SAFE_TOP and bottom <= to.SAFE_BOTTOM
    center = (top + bottom) / 2 / 1920
    assert abs(center - to.POSITIONS[pos]) < 0.06


@needs_fonts
def test_png_written_to_path(tmp_path):
    path = tmp_path / "o.png"
    data = to.render_overlay_png("テスト", "middle", "m", out_path=str(path))
    assert path.read_bytes() == data


# ── linter ───────────────────────────────────────────────────────────────────

GOOD = dict(
    title="I rebuilt combat after one review — Project Apocalypse",
    description="Turn-based tactics in a ruined city. Wishlist on Steam: {steam}",
    duration=18.0,
    overlays=[
        {"text": "I rebuilt combat after one review", "start": 0, "end": 2.5, "pos": "upper", "size": "l"},
        {"text": "Now it feels fair", "start": 6, "end": 9, "pos": "middle", "size": "m"},
        {"text": "Project Apocalypse — Wishlist on Steam", "start": 15, "end": 18, "pos": "middle", "size": "l"},
    ],
    project_name="Project Apocalypse", goal_id=3, platforms=["yt"], music_path="m.mp3",
)


def lint(**over):
    return sl.lint_clip(**{**GOOD, **over})


@needs_fonts
def test_good_clip_has_no_warnings():
    f = lint()
    assert codes(f, "warn") == set() and codes(f, "error") == set()
    assert {"duration", "hook_missing", "hook_length", "text_fit", "text_safe_zone", "text_timing",
            "name_end", "name_title", "title_ok", "desc_ok", "lang_mismatch", "goal_missing", "repeat"} <= codes(f, "ok")
    assert sl.summarize(f)["warn"] == 0


@pytest.mark.parametrize("dur,level", [(18, "ok"), (10, "warn"), (30, "warn"), (4, "error"), (70, "error"), (None, "info")])
def test_duration_levels(dur, level):
    f = [x for x in lint(duration=dur) if x["code"] == "duration"]
    assert f[0]["level"] == level


def test_hook_missing():
    ov = [dict(GOOD["overlays"][1])]
    assert "hook_missing" in codes(lint(overlays=ov), "warn")


def test_hook_length_words_and_japanese():
    ov = [{"text": "Wow", "start": 0, "end": 2}]
    assert "hook_length" in codes(lint(overlays=ov), "warn")
    ov = [{"text": "one two three four five six seven eight nine", "start": 0, "end": 2}]
    assert "hook_length" in codes(lint(overlays=ov), "warn")
    ov = [{"text": "敵が強すぎたので直した", "start": 0, "end": 2}]
    assert "hook_length" in codes(lint(overlays=ov, title="敵が強すぎたので直した Project Apocalypse"), "ok")
    ov = [{"text": "敵が強すぎたので戦闘システムを全部作り直しました本当に", "start": 0, "end": 2}]
    assert "hook_length" in codes(lint(overlays=ov), "warn")


@needs_fonts
def test_text_fit_and_safe_zone():
    long = {"text": "word " * 24, "start": 3, "end": 6, "pos": "lower", "size": "l"}
    assert "text_fit" in codes(lint(overlays=GOOD["overlays"] + [long]), "warn")
    bad = {"text": "down here", "start": 3, "end": 6, "pos": "bottom", "size": "m"}
    assert "text_safe_zone" in codes(lint(overlays=GOOD["overlays"] + [bad]), "error")


def test_text_overlap_short_and_after_end():
    ov = GOOD["overlays"] + [{"text": "Clash", "start": 7, "end": 8.5, "pos": "middle", "size": "m"}]
    assert "text_overlap" in codes(lint(overlays=ov), "warn")
    ov = GOOD["overlays"] + [{"text": "Blink", "start": 10, "end": 10.5, "pos": "lower", "size": "m"}]
    assert "text_short" in codes(lint(overlays=ov), "warn")
    ov = GOOD["overlays"] + [{"text": "Ghost", "start": 19, "end": 21, "pos": "lower", "size": "m"}]
    assert "text_after_end" in codes(lint(overlays=ov), "warn")


def test_game_name_at_end_and_in_title():
    ov = GOOD["overlays"][:2]
    f = lint(overlays=ov, title="I rebuilt combat after one review")
    assert {"name_end", "name_title"} <= codes(f, "warn")
    early = GOOD["overlays"][:2] + [{"text": "Project Apocalypse", "start": 3, "end": 5, "pos": "lower"}]
    assert "name_end" in codes(lint(overlays=early), "warn")
    f = lint(project_name="")
    assert "name_skip" in codes(f, "info") and not ({"name_end", "name_title"} & codes(f))


def test_title_rules():
    assert "title_empty" in codes(lint(title=""), "error")
    assert "title_hashtag" in codes(lint(title="#shorts #gamedev Project Apocalypse"), "warn")
    assert "title_label" in codes(lint(title="Level design"), "warn")
    assert "title_visible" in codes(lint(), "info")  # > 40 chars → shows what the player shows
    assert "title_visible" not in codes(lint(title="Short hook — Project Apocalypse"))


@pytest.mark.parametrize("title", [
    "Prototyping Hovercar Simulator", "More layout changes", "Final result of the initial pass",
    "Remodelling the walls :D", "Rebuilding the floor", "WIP: new boss", "Devlog #12 — Project Apocalypse",
    "Прототип ховеркара — Project Apocalypse", "レイアウト作業中 Project Apocalypse",
])
def test_dev_language_titles(title):
    assert "title_dev" in codes(lint(title=title), "warn")


@pytest.mark.parametrize("title", [
    "Would you drive this through a warzone?", "This hovercar was a terrible idea",
    "Day 1 vs Day 30 of my hovercar game", "I rebuilt combat after one review — Project Apocalypse",
    "Hovercar vs. the entire city",
])
def test_hook_titles_are_not_dev_language(title):
    assert "title_dev" not in codes(lint(title=title))


def test_fade_in_kills_the_first_frame_sound():
    assert "fade_in" in codes(lint(fade_in=1.0), "warn")
    assert "fade_in" not in codes(lint(fade_in=0)) and "fade_in" not in codes(lint())
    res = sl.lint_batch([{"title": "x"}], fade_in="2")
    assert "fade_in" in codes(res[0]["findings"], "warn")
    # a card's own fade (its music under a muted game) replaces the batch value
    res = sl.lint_batch([{"title": "x", "fade_in": 0}, {"title": "y", "fade_in": 1.5}], fade_in="2")
    assert "fade_in" not in codes(res[0]["findings"]) and "fade_in" in codes(res[1]["findings"], "warn")


def test_description_rules():
    assert "desc_empty" in codes(lint(description=""), "warn")
    assert "desc_empty" in codes(lint(description="{steam}  https://x.io"), "warn")  # a bare link says nothing
    # the user's case: the title copied into the description (TikTok shows both in a row)
    t = "Protecting the trade caravan"
    assert "desc_repeat" in codes(lint(title=t, description="Protecting the trade caravan from raiders"), "warn")
    assert "desc_repeat" in codes(lint(title=t, description="protecting the trade caravan!"), "warn")
    assert "desc_ok" in codes(lint(title=t, description="Wasteland Courier — a freelancer sim. Raiders hit the road."), "ok")
    assert "desc_repeat" not in codes(lint(title="#Shorts", description="anything"))


def test_language_consistency():
    f = lint(title="Я переделал бой после одного отзыва — Project Apocalypse")
    assert "lang_mismatch" in codes(f, "warn")
    ru = [{"text": "Я переделал бой за неделю", "start": 0, "end": 2.5, "pos": "upper", "size": "l"},
          {"text": "Project Apocalypse — Wishlist on Steam", "start": 15, "end": 18, "pos": "middle"}]
    f = lint(title="Я переделал бой после одного отзыва — Project Apocalypse", overlays=ru)
    assert "lang_mismatch" in codes(f, "ok")  # the Steam end card is fine on a Russian clip


def test_goal():
    assert "goal_missing" in codes(lint(goal_id=None), "warn")
    assert "goal_missing" in codes(lint(goal_id=""), "warn")


def test_repetition_batch_and_recent():
    assert "repeat" in codes(lint(batch_titles=["I rebuilt combat after one review — Project Apocalypse!"]), "warn")
    assert "repeat" in codes(lint(batch_hooks=["i rebuilt COMBAT after one review"]), "warn")
    assert "repeat" in codes(lint(recent_titles=["I rebuilt combat after one review - Project Apocalypse"]), "warn")
    assert "repeat" in codes(lint(recent_titles=["Totally different", "Other one", "Third"]), "ok")
    assert sl.normalize("#Shorts Hello, World!") == "helloworld"


def test_music_hint_for_youtube():
    assert "music_youtube" in codes(lint(music_path=""), "info")
    assert "music_youtube" not in codes(lint(music_path="", platforms=["tiktok"]))


def test_errors_and_warnings_come_first():
    f = lint(title="", goal_id=None)
    levels = [x["level"] for x in f]
    assert levels == sorted(levels, key=lambda l: sl.LEVEL_ORDER[l])


def test_lint_batch_compares_cards():
    card = {"title": GOOD["title"], "duration": 18, "overlays": GOOD["overlays"], "music_path": "m"}
    res = sl.lint_batch([card, dict(card)], project_name="Project Apocalypse", goal_id=1, platforms=["yt"])
    assert len(res) == 2
    assert all("repeat" in codes(r["findings"], "warn") for r in res)
    assert res[0]["counts"]["warn"] >= 1


# ── endpoints ────────────────────────────────────────────────────────────────

def _project(session, name):
    from app.models.models import Project
    p = Project(user_id=1, name=name)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def test_lint_endpoint_uses_project_and_recent_titles(client, session):
    from app.models.models import Brief, Post, PostStatusEnum
    p = _project(session, "Lint Game")
    b = Brief(user_id=1, type="video", title="Same old hook — Lint Game", project_id=p.id)
    session.add(b)
    session.commit()
    session.add(Post(user_id=1, brief_id=b.id, platform="yt", status=PostStatusEnum.done))
    session.commit()
    body = {"project_id": p.id, "goal_id": None, "platforms": ["yt"], "cards": [
        {"title": "Same old hook — Lint Game", "duration": 18, "overlays": [], "music_path": ""},
        {"title": "", "duration": 3, "overlays": []},
    ]}
    r = client.post("/api/shorts/lint", json=body)
    assert r.status_code == 200
    j = r.json()
    assert j["project_name"] == "Lint Game" and len(j["cards"]) == 2
    first = j["cards"][0]["findings"]
    assert "repeat" in codes(first, "warn") and "goal_missing" in codes(first, "warn")
    assert "name_title" in codes(first, "ok")
    assert {"title_empty", "duration"} <= codes(j["cards"][1]["findings"], "error")
    assert j["cards"][1]["counts"]["error"] >= 2
    assert client.post("/api/shorts/lint", json={}).json()["cards"] == []


def test_queue_end_finds_last_scheduled_short(client, session):
    """Queued posts of factory briefs + clips still rendering count; done/canceled posts,
    finished render jobs and non-factory briefs don't. 2099 dates outrun other tests' data."""
    from datetime import datetime
    from sqlmodel import select
    from app.models.models import Brief, Job, JobKindEnum, JobStatusEnum, Post, PostStatusEnum

    def brief(title, factory=True):
        b = Brief(user_id=1, type="video", title=title, vertical_916=factory)
        session.add(b)
        session.commit()
        session.refresh(b)
        return b

    def post(b, platform, when, status=PostStatusEnum.queued):
        session.add(Post(user_id=1, brief_id=b.id, platform=platform, status=status, scheduled_at=when))

    def render_job(b, when, status=JobStatusEnum.queued):
        session.add(Job(kind=JobKindEnum.render, status=status,
                        payload_json=json.dumps({"brief_id": b.id, "platforms": ["yt"], "scheduled_at": when})))

    a = brief("Queue A")
    post(a, "yt", datetime(2099, 1, 1, 10, 0))
    post(a, "vk_video", datetime(2099, 1, 1, 12, 0))
    gone = brief("Queue gone")
    post(gone, "yt", datetime(2099, 6, 1), status=PostStatusEnum.done)
    post(gone, "tiktok", datetime(2099, 7, 1), status=PostStatusEnum.canceled)
    post(brief("Not a short", factory=False), "yt", datetime(2099, 8, 1))
    rendering = brief("Queue rendering")
    render_job(rendering, "2099-02-01T09:30")
    render_job(gone, "2099-09-01T00:00", status=JobStatusEnum.done)
    session.commit()

    j = client.get("/api/shorts/queue_end").json()
    assert j["last"] == "2099-02-01T09:30:00" and j["title"] == "Queue rendering"
    assert j["queued"] >= 2

    job = session.exec(select(Job).where(Job.payload_json.contains('"2099-02-01T09:30"'))).one()
    job.status = JobStatusEnum.done
    session.add(job)
    session.commit()
    j = client.get("/api/shorts/queue_end").json()
    assert j["last"] == "2099-01-01T12:00:00" and j["title"] == "Queue A"

    # leave nothing queued for the other tests
    for p in session.exec(select(Post).where(Post.status == PostStatusEnum.queued,
                                             Post.scheduled_at >= datetime(2099, 1, 1))).all():
        p.status = PostStatusEnum.canceled
        session.add(p)
    session.commit()


@needs_fonts
def test_overlay_preview_endpoint(client):
    r = client.post("/api/shorts/overlay_preview", json={"text": "Hello Steam", "pos": "upper", "size": "l"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (1080, 1920)
    _l, top, _r, bottom = img.getchannel("A").getbbox()
    assert abs((top + bottom) / 2 / 1920 - to.POSITIONS["upper"]) < 0.06
    # bad pos/size fall back to safe defaults instead of failing
    assert client.post("/api/shorts/overlay_preview", json={"text": "x", "pos": "floor", "size": "xxl"}).status_code == 200


# ── factory submit + render wiring ───────────────────────────────────────────

def test_submit_shorts_stores_trim_and_overlays(client, session, tmp_path, monkeypatch):
    from sqlmodel import select
    from app.jobs import tasks
    from app.models.models import Brief
    scheduled = []
    monkeypatch.setattr(tasks.render_short, "schedule", lambda **kw: scheduled.append(kw))
    monkeypatch.chdir(tmp_path)  # the source clip lands in tmp/media, not the real media/
    meta = [{
        "title": "Editor test clip", "tags": "#a", "text_en": "d", "platforms": ["yt"],
        "trim_start": 5, "trim_end": 25,
        "overlays": [
            {"text": "Hook line here", "start": 0, "end": 2.5, "pos": "upper", "size": "l"},
            {"text": "", "start": 3, "end": 4},
            {"text": "Too late", "start": 30, "end": 40},
            {"text": "End card", "start": 17, "end": 99, "pos": "middle", "size": "l"},
        ],
    }, {"title": "Plain clip", "platforms": ["yt"]}]
    files = [("files", ("a.mp4", b"fake-video-a", "video/mp4")), ("files", ("b.mp4", b"fake-video-b", "video/mp4"))]
    r = client.post("/shorts", data={"meta": json.dumps(meta)}, files=files, follow_redirects=False)
    assert r.status_code == 303
    assert len(scheduled) == 2
    assert (tmp_path / "media").is_dir() and len(os.listdir(tmp_path / "media")) == 2
    ids = [kw["args"][1] for kw in scheduled]
    b1, b2 = (session.get(Brief, i) for i in ids)
    assert (b1.trim_start, b1.trim_end) == (5.0, 25.0)
    ovs = json.loads(b1.overlays_json)
    assert [o["text"] for o in ovs] == ["Hook line here", "End card"]
    assert ovs[1]["end"] == 20.0  # clamped to the trimmed length
    assert b2.trim_start is None and b2.trim_end is None and b2.overlays_json is None


def test_short_edit_suffix_changes_with_edits():
    from types import SimpleNamespace as NS
    from app.jobs.tasks import _short_edit
    plain = _short_edit(NS(trim_start=None, trim_end=None, overlays_json=None))
    assert plain["suffix"] == "" and plain["overlays"] == []
    ov = json.dumps([{"text": "Hi", "start": 0, "end": 2}])
    a = _short_edit(NS(trim_start=5, trim_end=25, overlays_json=ov))
    b = _short_edit(NS(trim_start=5, trim_end=24, overlays_json=ov))
    assert a["suffix"] and a["suffix"] != b["suffix"]
    assert a["trim_start"] == 5 and a["trim_end"] == 25 and a["overlays"][0]["text"] == "Hi"


def test_render_short_passes_trim_and_overlays(session, tmp_path, monkeypatch):
    """render_short writes overlay PNGs to a temp dir, passes them + the trim to
    ffmpeg, cleans up, and doesn't reuse an old unedited render."""
    from app.jobs import tasks
    from app.models.models import Brief, Job, JobKindEnum
    from app.services import video_editor
    monkeypatch.chdir(tmp_path)
    os.makedirs("media")
    open("media/short_x_clip.mp4", "wb").write(b"src")
    open("media/short_x_clip_vertical.mp4", "wb").write(b"old render")
    b = Brief(user_id=1, type="video", media_path="short_x_clip.mp4", vertical_916=True,
              media_vertical_path="short_x_clip_vertical.mp4", trim_start=2, trim_end=12,
              overlays_json=json.dumps([{"text": "Hook", "start": 0, "end": 2, "pos": "upper", "size": "l"}]))
    session.add(b)
    session.commit()
    job = Job(kind=JobKindEnum.render, payload_json=json.dumps({"brief_id": b.id, "platforms": []}))
    session.add(job)
    session.commit()
    calls = []

    def fake_render(src, out, **kw):
        calls.append(kw)
        assert all(os.path.exists(p) for p, _s, _e in kw["overlays"])
        open(out, "wb").write(b"new")
    monkeypatch.setattr(video_editor, "render_vertical_with_music", fake_render)
    monkeypatch.setattr(video_editor, "extract_poster_frame", lambda *a, **k: None)
    monkeypatch.setattr(to, "render_overlay_png",
                        lambda text, pos, size, out_path=None: open(out_path, "wb").write(b"png"))
    tasks.render_short.call_local(job.id, b.id)
    assert len(calls) == 1
    kw = calls[0]
    assert kw["start_time"] == 2 and kw["end_time"] == 12
    png, s, e = kw["overlays"][0]
    assert (s, e) == (0, 2) and not os.path.exists(png)  # temp PNGs cleaned up
    session.refresh(b)
    assert b.media_vertical_path.startswith("short_x_clip_vertical_") and b.media_vertical_path.endswith(".mp4")


# ── factory with the shared video editor: upload on drop, spec per clip ──────

def _ffmpeg_bin():
    try:
        from app.services import video_edit
        return video_edit._ffmpeg()
    except Exception:
        return None


needs_ffmpeg = pytest.mark.skipif(_ffmpeg_bin() is None, reason="no ffmpeg")
FFMPEG_ABS = _ffmpeg_bin()


@pytest.fixture
def abs_ffmpeg(monkeypatch):
    """The bundled ffmpeg is found relative to the project root; these tests chdir away."""
    from app.services import video_editor
    monkeypatch.setattr(video_editor, "_ffmpeg_path", lambda: FFMPEG_ABS)


FAKE_INFO ={"duration": 20.0, "width": 1920, "height": 1080, "has_audio": True, "fps": 30.0, "size_mb": 1.0}


def _make_clip(path, seconds=3):
    import subprocess
    subprocess.run([_ffmpeg_bin(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"testsrc2=size=640x360:rate=25:duration={seconds}", "-f", "lavfi",
                    "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-preset", "ultrafast",
                    "-c:a", "aac", "-shortest", str(path)], check=True, capture_output=True)


def test_upload_refuses_a_non_video(client, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    r = client.post("/api/shorts/upload", files={"file": ("notes.mp4", b"not a video", "video/mp4")})
    assert r.status_code == 400
    assert not os.listdir(tmp_path / "media")  # nothing left behind


@needs_ffmpeg
def test_upload_probe_and_delete(client, session, tmp_path, monkeypatch, abs_ffmpeg):
    from app.models.models import Brief
    src = tmp_path / "clip.mp4"
    _make_clip(src)
    monkeypatch.chdir(tmp_path)
    with open(src, "rb") as fh:
        r = client.post("/api/shorts/upload", files={"file": ("My Replay 01.mp4", fh, "video/mp4")})
    assert r.status_code == 200
    j = r.json()
    assert j["rel"].startswith("short_") and j["rel"].endswith("_My_Replay_01.mp4")
    assert (j["width"], j["height"], j["has_audio"]) == (640, 360, True) and abs(j["duration"] - 3) < 0.2
    assert j["url"] == f"/media/{j['rel']}" and (tmp_path / "media" / j["rel"]).is_file()
    # removed from the page before the submit: the file goes too, once
    assert client.delete("/api/shorts/upload", params={"rel": j["rel"]}).json() == {"ok": True}
    assert not (tmp_path / "media" / j["rel"]).exists()
    assert client.delete("/api/shorts/upload", params={"rel": j["rel"]}).json() == {"ok": False}
    # never a file a brief uses, nor anything that is not a factory upload
    (tmp_path / "media" / "short_used_clip.mp4").write_bytes(b"x")
    (tmp_path / "media" / "poster.jpg").write_bytes(b"x")
    session.add(Brief(user_id=1, type="video", media_path="short_used_clip.mp4", vertical_916=True))
    session.commit()
    for rel in ("short_used_clip.mp4", "poster.jpg", "../clip.mp4"):
        assert client.delete("/api/shorts/upload", params={"rel": rel}).json() == {"ok": False}
    assert (tmp_path / "media" / "short_used_clip.mp4").exists() and (tmp_path / "media" / "poster.jpg").exists()


def test_discard_on_page_close(client, session, tmp_path, monkeypatch):
    from app.models.models import Brief
    monkeypatch.chdir(tmp_path)
    (tmp_path / "media").mkdir()
    for name in ("short_a1_clip.mp4", "short_b2_clip.mp4", "short_used_x.mp4", "keep.mp4"):
        (tmp_path / "media" / name).write_bytes(b"x")
    session.add(Brief(user_id=1, type="video", media_path="short_used_x.mp4", vertical_916=True))
    session.commit()
    rels = ["short_a1_clip.mp4", "short_b2_clip.mp4", "short_used_x.mp4", "keep.mp4", "../etc"]
    r = client.post("/api/shorts/upload/discard", json={"rels": rels})
    assert r.json() == {"deleted": 2}
    assert sorted(os.listdir(tmp_path / "media")) == ["keep.mp4", "short_used_x.mp4"]
    assert client.post("/api/shorts/upload/discard", json={"rels": "nope"}).json() == {"deleted": 0}


def test_submit_with_uploaded_clips_stores_the_editor_spec(client, session, tmp_path, monkeypatch, set_settings):
    from app.jobs import tasks
    from app.models.models import Brief
    from app.services import video_edit
    scheduled = []
    monkeypatch.setattr(tasks.render_short, "schedule", lambda **kw: scheduled.append(kw))
    monkeypatch.setattr(video_edit, "probe", lambda path: dict(FAKE_INFO))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "media").mkdir()
    for name in ("short_aaa_clip.mp4", "short_bbb_clip.mp4", "foreign.mp4"):
        (tmp_path / "media" / name).write_bytes(b"src")
    lib = tmp_path / "Music"
    lib.mkdir()
    (lib / "drive.mp3").write_bytes(b"x")
    (tmp_path / "secret.mp3").write_bytes(b"x")
    set_settings(music_dir=str(lib))
    spec = {"format": "landscape", "crop_x": 0.3, "crop_y": 0.5, "zoom": 1.5, "trim_start": 2, "trim_end": 14,
            "source_volume": 0.4, "overlays": [{"text": "Hook", "start": 0, "end": 2, "pos": "upper", "size": "l"}],
            "music": {"path": str(lib / "drive.mp3"), "start": 3.5, "volume": 0.7, "fade_in": 0, "fade_out": 1}}
    meta = [
        {"src_rel": "short_aaa_clip.mp4", "title": "Raiders hit the caravan", "text_en": "d", "tags": "#a",
         "platforms": ["yt"], "spec": spec},
        {"src_rel": "short_bbb_clip.mp4", "title": "Foreign track", "platforms": ["yt"],
         "spec": {**spec, "music": {"path": str(tmp_path / "secret.mp3")}}},
        {"src_rel": "foreign.mp4", "title": "not an upload", "platforms": ["yt"], "spec": spec},
        {"src_rel": "../short_aaa_clip.mp4", "title": "escape", "platforms": ["yt"], "spec": spec},
    ]
    r = client.post("/shorts", data={"meta": json.dumps(meta)}, follow_redirects=False)
    assert r.status_code == 303
    assert len(scheduled) == 2
    b1, b2 = (session.get(Brief, kw["args"][1]) for kw in scheduled)
    s1 = json.loads(b1.edit_spec_json)
    assert s1["format"] == "vertical"  # the factory only makes 9:16
    assert (s1["crop_x"], s1["zoom"], s1["trim_start"], s1["trim_end"], s1["source_volume"]) == (0.3, 1.5, 2.0, 14.0, 0.4)
    assert s1["music"] == {"path": str(lib / "drive.mp3"), "start": 3.5, "volume": 0.7, "fade_in": 0.0, "fade_out": 1.0}
    assert s1["overlays"][0]["text"] == "Hook"
    # mirrored into the old columns
    assert (b1.media_path, b1.crop_x, b1.crop_y, b1.crop_scale) == ("short_aaa_clip.mp4", 30, 50, 1.5)
    assert (b1.trim_start, b1.trim_end, b1.music_path, b1.music_offset_sec) == (2.0, 14.0, str(lib / "drive.mp3"), 3)
    assert json.loads(b1.overlays_json)[0]["text"] == "Hook"
    assert (b1.title, b1.text_en, b1.tags_json) == ("Raiders hit the caravan", "d", "#a")
    # a track outside the music folders is dropped, the clip still goes
    assert json.loads(b2.edit_spec_json)["music"] is None and b2.music_path is None


def test_render_short_renders_the_editor_spec(session, tmp_path, monkeypatch):
    """A clip with an editor spec goes through video_edit.render_file (one pass, the
    preview's geometry); a track that disappeared is dropped instead of failing."""
    from app.jobs import tasks
    from app.models.models import Brief, Job, JobKindEnum
    from app.services import video_edit, video_editor
    monkeypatch.chdir(tmp_path)
    os.makedirs("media")
    open("media/short_x_clip.mp4", "wb").write(b"src")
    spec = {"format": "vertical", "zoom": 2, "crop_x": 0.1, "trim_start": 1, "trim_end": 9,
            "music": {"path": str(tmp_path / "gone.mp3")}}
    b = Brief(user_id=1, type="video", media_path="short_x_clip.mp4", vertical_916=True,
              edit_spec_json=json.dumps(spec))
    session.add(b)
    session.commit()
    job = Job(kind=JobKindEnum.render, payload_json=json.dumps({"brief_id": b.id, "platforms": []}))
    session.add(job)
    session.commit()
    calls = []

    def fake_render(src, out, spec, info=None, **kw):
        calls.append((src, out, spec))
        open(out, "wb").write(b"new")
    monkeypatch.setattr(video_edit, "probe", lambda path: dict(FAKE_INFO))
    monkeypatch.setattr(video_edit, "render_file", fake_render)
    monkeypatch.setattr(video_editor, "render_vertical_with_music", lambda *a, **k: pytest.fail("old renderer"))
    monkeypatch.setattr(video_editor, "extract_poster_frame", lambda *a, **k: None)
    tasks.render_short.call_local(job.id, b.id)
    assert len(calls) == 1
    src, out, used = calls[0]
    assert used["music"] is None and used["zoom"] == 2.0 and used["crop_x"] == 0.1
    assert (used["trim_start"], used["trim_end"]) == (1.0, 9.0)
    session.refresh(b)
    assert b.media_vertical_path == f"short_x_clip_vertical_{video_edit.spec_hash(used)}.mp4"
    assert os.path.basename(out) == b.media_vertical_path
    # the same settings again: the render is reused
    tasks.render_short.call_local(job.id, b.id)
    assert len(calls) == 1


@needs_ffmpeg
@needs_fonts
def test_render_short_spec_real_ffmpeg(session, tmp_path, monkeypatch, abs_ffmpeg):
    from app.jobs import tasks
    from app.models.models import Brief, Job, JobKindEnum
    from app.services import video_edit
    monkeypatch.chdir(tmp_path)
    os.makedirs("media")
    _make_clip(tmp_path / "media" / "short_y_src.mp4")
    spec = {"zoom": 1.3, "crop_x": 0.8, "trim_start": 0.5, "source_volume": 0.5,
            "overlays": [{"text": "Hook!", "start": 0, "end": 1, "pos": "upper", "size": "l"}]}
    b = Brief(user_id=1, type="video", media_path="short_y_src.mp4", vertical_916=True,
              edit_spec_json=json.dumps(spec))
    session.add(b)
    session.commit()
    job = Job(kind=JobKindEnum.render, payload_json=json.dumps({"brief_id": b.id, "platforms": []}))
    session.add(job)
    session.commit()
    assert tasks.render_short.call_local(job.id, b.id).startswith("rendered")
    session.refresh(b)
    out = video_edit.probe(os.path.join("media", b.media_vertical_path))
    assert (out["width"], out["height"], out["has_audio"]) == (1080, 1920, True)
    assert abs(out["duration"] - 2.5) < 0.2
    assert os.path.exists("media/short_y_src_poster.jpg")
    assert not [f for f in os.listdir("media") if f.endswith(".part")]
