"""Video editor for every page that takes a video: settings validation, the ffmpeg
command, renders (real ffmpeg when available), the music library and its file guard,
the "fits the platform?" report, and the Create Post / Create Video forms."""
import io
import json
import os
import subprocess
import time
import uuid

import pytest
from PIL import Image
from sqlmodel import Session, select

from app.db.session import engine
from app.models.models import Brief, Job, Post
from app.services import music_library as ml
from app.services import text_overlay as to
from app.services import video_edit as ve
from app.services.video_editor import compression_plan, max_fit_duration


def _ffmpeg():
    try:
        return ve._ffmpeg()
    except Exception:
        return None


needs_ffmpeg = pytest.mark.skipif(_ffmpeg() is None, reason="no ffmpeg")
needs_fonts = pytest.mark.skipif(not to._candidates(False), reason="no system fonts")

INFO = {"duration": 30.0, "width": 1920, "height": 1080, "has_audio": True, "fps": 60, "size_mb": 40}


def spec(**kw):
    return ve.clean_spec(kw, INFO["duration"])


# ── settings from the page ─────────────────────────────────────────────────────

def test_clean_spec_clamps_everything():
    s = ve.clean_spec({"format": "cinema", "crop_x": 7, "crop_y": -1, "zoom": 99, "source_volume": "x",
                       "trim_start": 29.8, "trim_end": 40,
                       "music": {"path": "  ", "volume": 5}}, 30)
    assert s["format"] == "original"
    assert (s["crop_x"], s["crop_y"], s["zoom"]) == (1.0, 0.0, ve.MAX_ZOOM)
    assert s["source_volume"] == 1.0
    assert s["trim_start"] is None and s["trim_end"] is None  # start too late, end past the source
    assert s["music"] is None                                 # blank path = no music

    s = ve.clean_spec({"format": "vertical", "trim_start": 5, "trim_end": 15,
                       "music": {"path": "a.mp3", "start": -3, "volume": 9, "fade_out": 99},
                       "overlays": [{"text": "late", "start": 8, "end": 30}]}, 30)
    assert (s["trim_start"], s["trim_end"]) == (5.0, 15.0)
    assert s["music"] == {"path": "a.mp3", "start": 0.0, "volume": 2.0, "fade_in": 0.5, "fade_out": 10.0}
    assert s["overlays"][0]["end"] == 10.0  # clamped to the 10 s clip


def test_output_size():
    assert ve.output_size("vertical", 1920, 1080) == (1080, 1920)
    assert ve.output_size("landscape", 1080, 1920) == (1920, 1080)
    assert ve.output_size("square", 640, 480) == (1080, 1080)
    assert ve.output_size("original", 3840, 2160) == (1920, 1080)   # 4K is scaled down
    assert ve.output_size("original", 1280, 720) == (1280, 720)     # never upscaled
    w, h = ve.output_size("original", 1081, 1921)
    assert w % 2 == 0 and h % 2 == 0


def test_noop_keeps_the_file():
    assert ve.is_noop(spec(format="original"), INFO)
    assert not ve.is_noop(spec(format="original", trim_start=2), INFO)
    assert not ve.is_noop(spec(format="vertical"), INFO)
    assert not ve.is_noop(spec(format="original", source_volume=0), INFO)
    assert not ve.is_noop(spec(format="original"), {**INFO, "width": 3840, "height": 2160})


# ── the ffmpeg command ───────────────────────────────────────────────────────────

def _fc(cmd):
    return cmd[cmd.index("-filter_complex") + 1]


def test_command_vertical_with_music_and_text():
    s = spec(format="vertical", crop_x=0.25, zoom=1.5, trim_start=4, trim_end=19,
             music={"path": "m.mp3", "start": 12, "volume": 0.6, "fade_in": 1, "fade_out": 2},
             overlays=[{"text": "Hook", "start": 0, "end": 2}])
    cmd = ve.build_command("ffmpeg", "src.mp4", "out.mp4", s, INFO,
                           [("o.png", 0, 2)], "m.mp3", music_duration=200)
    fc = _fc(cmd)
    i = cmd.index("src.mp4")
    assert cmd[i - 5:i - 1] == ["-ss", "4.000", "-t", "15.000"]   # input trim
    assert cmd[cmd.index("m.mp3") - 3:cmd.index("m.mp3") - 1] == ["-ss", "12.000"]
    assert "-stream_loop" not in cmd                            # the track is long enough
    assert "scale=1620:2880:force_original_aspect_ratio=increase" in fc
    assert "crop=1080:1920:(iw-ow)*0.2500" in fc
    assert "overlay=0:0:enable='between(t,0.000,2.000)'" in fc
    assert "[0:a]volume=1.00[as]" in fc
    assert "afade=t=out:st=13.000:d=2.00" in fc                 # fades end with the clip
    assert "amix=inputs=2" in fc and "normalize=0" in fc
    assert cmd[cmd.index("-t", i) + 1] == "15.000"
    assert "[a]" in cmd and "-an" not in cmd


def test_command_loops_a_short_track_and_mutes_the_game():
    s = spec(format="landscape", source_volume=0, music={"path": "m.mp3"})
    cmd = ve.build_command("ffmpeg", "src.mp4", "out.mp4", s, INFO, None, "m.mp3", music_duration=10)
    assert cmd[cmd.index("m.mp3") - 3:cmd.index("m.mp3") - 1] == ["-stream_loop", "-1"]
    fc = _fc(cmd)
    assert "[0:a]" not in fc and "[am]anull[a]" in fc
    assert "crop=1920:1080" in fc


def test_command_without_any_audio():
    cmd = ve.build_command("ffmpeg", "src.mp4", "out.mp4", spec(format="square"),
                           {**INFO, "has_audio": False})
    assert "-an" in cmd and "[a]" not in cmd


def test_output_name_follows_the_settings():
    a = ve.output_rel("1790_My clip (1).mp4", spec(format="vertical"))
    b = ve.output_rel("1790_My clip (1).mp4", spec(format="vertical", trim_start=1))
    assert a.startswith("1790_My_clip_1_9x16_") and a.endswith(".mp4") and a != b
    assert ve.output_rel("1790_My clip (1).mp4", spec(format="vertical")) == a


# ── paths ───────────────────────────────────────────────────────────────────────

def test_media_abs_stays_inside_media(tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "MEDIA_DIR", str(tmp_path))
    (tmp_path / "ok.mp4").write_bytes(b"x")
    assert ve.media_abs("ok.mp4") == os.path.realpath(tmp_path / "ok.mp4")
    assert ve.media_abs("media/ok.mp4") == ve.media_abs("/media/ok.mp4") == ve.media_abs("ok.mp4")
    for bad in ("../settings.json", "", "missing.mp4", os.path.abspath(__file__), "a/../../x"):
        with pytest.raises(ve.EditError):
            ve.media_abs(bad)


# ── music library ────────────────────────────────────────────────────────────────

@pytest.fixture
def music(tmp_path, set_settings):
    a, b = tmp_path / "Music A", tmp_path / "Music B"
    (a / "Calm").mkdir(parents=True)
    b.mkdir()
    (a / "drop.mp3").write_bytes(b"x")
    (a / "Calm" / "pad.wav").write_bytes(b"x")
    (a / "notes.txt").write_bytes(b"x")
    (b / "beat.ogg").write_bytes(b"x")
    set_settings(music_dir=f"{a}\n{b};{tmp_path / 'gone'}")
    return a, b


def test_music_folders_and_tracks(music):
    a, b = music
    assert ml.folders() == [str(a), str(b), str(a.parent / "gone")]
    tracks = ml.list_tracks()
    assert [(t["folder"], t["name"]) for t in tracks] == [
        ("Music A", "drop.mp3"), ("Music A / Calm", "pad.wav"), ("Music B", "beat.ogg")]


def test_music_guard(music, tmp_path):
    a, _b = music
    assert ml.is_allowed(str(a / "Calm" / "pad.wav"))
    assert not ml.is_allowed(str(a / "notes.txt"))                 # not audio
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"x")
    assert not ml.is_allowed(str(outside))
    assert ml.is_allowed(str(outside), extra=[str(outside)])        # a project's own track
    assert not ml.is_allowed(str(a / ".." / "secret.mp3"))


def test_music_endpoints(client, music, tmp_path):
    a, _b = music
    j = client.get("/api/music/list").json()
    assert j["ok"] and len(j["tracks"]) == 3
    assert [f["exists"] for f in j["folders"]] == [True, True, False]
    assert client.get("/api/music/file", params={"path": str(a / "drop.mp3")}).status_code == 200
    outside = tmp_path / "secret.mp3"
    outside.write_bytes(b"x")
    assert client.get("/api/music/file", params={"path": str(outside)}).status_code == 404
    assert client.get("/api/music/file", params={"path": os.path.abspath(".settings.json")}).status_code == 404


def test_render_refuses_a_track_outside_the_library(tmp_path, monkeypatch):
    monkeypatch.setattr(ve, "MEDIA_DIR", str(tmp_path))
    (tmp_path / "v.mp4").write_bytes(b"x")
    monkeypatch.setattr(ve, "probe", lambda p: dict(INFO))
    with pytest.raises(ve.EditError, match="music"):
        ve.start_render("v.mp4", {"format": "vertical", "music": {"path": "C:/x.mp3"}}, music_ok=lambda p: False)


# ── fits the platforms? ────────────────────────────────────────────────────────────

def _fit(monkeypatch, size_mb, duration, platforms, **cfg):
    monkeypatch.setattr(ve, "probe", lambda p: {**INFO, "size_mb": size_mb, "duration": duration})
    return {r["platform"]: r for r in ve.fit_report("x.mp4", platforms, cfg)["rows"]}


def test_fit_report(monkeypatch):
    rows = _fit(monkeypatch, 8, 20, ["discord", "tg", "nope"])
    assert set(rows) == {"discord", "tg"} and rows["discord"]["level"] == "ok"

    rows = _fit(monkeypatch, 40, 60, ["discord", "tg"])
    assert rows["discord"]["level"] in ("info", "warn") and "compressed to" in rows["discord"]["text"]
    assert rows["tg"]["level"] == "ok"

    rows = _fit(monkeypatch, 120, 300, ["discord", "x", "bluesky"])
    assert rows["discord"]["level"] == "error" and "won’t fit" in rows["discord"]["text"]
    assert rows["x"]["level"] == "error" and "2:20" in rows["x"]["text"]
    assert rows["bluesky"]["level"] == "error"

    rows = _fit(monkeypatch, 40, 300, ["discord"], discord_upload_limit_mb=50)  # boosted server
    assert rows["discord"]["level"] != "error"


def test_fit_matches_the_compressor():
    """The report and compress_video_to_size share compression_plan: a clip just under
    max_fit_duration still gets a bitrate above the floor, one just over doesn't."""
    from app.services.video_editor import MIN_VIDEO_KBPS
    limit = 9.5
    edge = max_fit_duration(limit)
    assert compression_plan(edge - 1, 1920, 1080, limit)["attempts"][0][0] >= MIN_VIDEO_KBPS
    assert compression_plan(edge + 2, 1920, 1080, limit)["attempts"][0][0] < MIN_VIDEO_KBPS


# ── text at any frame size ───────────────────────────────────────────────────────────

@needs_fonts
def test_overlay_png_at_other_frames(client):
    img = Image.open(io.BytesIO(to.render_overlay_png("Patch 1.2 is out", "lower", "m", frame=(1920, 1080))))
    assert img.size == (1920, 1080)
    box = img.getbbox()
    assert box and box[3] <= int(1080 * 0.94)                     # inside the margin
    r = client.post("/api/video/overlay_preview", json={"text": "Hi", "w": 1080, "h": 1080})
    assert r.status_code == 200 and Image.open(io.BytesIO(r.content)).size == (1080, 1080)
    assert to.frame_metrics() == {"w": 1080, "h": 1920, "scale": 1.0, "max_w": to.MAX_TEXT_W,
                                  "safe_top": to.SAFE_TOP, "safe_bottom": to.SAFE_BOTTOM, "pad": to.SAFE_PAD}


# ── real renders ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def clip(tmp_path, monkeypatch):
    """A 3 s 640×360 clip with sound in a throwaway media/ dir, and a 1 s beep track."""
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(ve, "MEDIA_DIR", str(media))
    monkeypatch.setattr(ve, "EDITS_DIR", str(tmp_path / "edits"))
    ff = _ffmpeg()
    run = lambda *a: subprocess.run([ff, "-v", "error", "-y", *a], check=True, capture_output=True)
    run("-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=3", "-f", "lavfi",
        "-i", "sine=frequency=440:duration=3", "-c:v", "libx264", "-preset", "ultrafast",
        "-c:a", "aac", "-shortest", str(media / "src.mp4"))
    run("-f", "lavfi", "-i", "sine=frequency=880:duration=1", str(tmp_path / "beep.wav"))
    return media, str(tmp_path / "beep.wav")


def _wait(job):
    t0 = time.time()
    while job["status"] in ("queued", "running") and time.time() - t0 < 60:
        time.sleep(0.2)
        job = ve.get_job(job["id"])
    return job


@needs_ffmpeg
@needs_fonts
def test_render_vertical_with_text_and_looped_music(clip):
    media, beep = clip
    raw = {"format": "vertical", "trim_start": 0.5, "source_volume": 0.5,
           "music": {"path": beep, "start": 0.2}, "overlays": [{"text": "Хук!", "start": 0, "end": 1}]}
    job = _wait(ve.start_render("src.mp4", raw, music_ok=lambda p: p == beep))
    assert job["status"] == "done", job.get("error")
    out = ve.probe(ve.media_abs(job["out_rel"]))
    assert (out["width"], out["height"], out["has_audio"]) == (1080, 1920, True)
    assert abs(out["duration"] - 2.5) < 0.2
    edit = ve.load_edit(job["out_rel"])
    assert edit["src"] == "src.mp4" and edit["spec"]["format"] == "vertical"
    again = ve.start_render("src.mp4", raw, music_ok=lambda p: True)   # same settings: no new render
    assert again["status"] == "done" and again["out_rel"] == job["out_rel"]
    assert not list(media.glob("*.part"))


@needs_ffmpeg
def test_render_landscape_without_sound_and_noop(clip):
    job = _wait(ve.start_render("src.mp4", {"format": "landscape", "source_volume": 0}))
    assert job["status"] == "done", job.get("error")
    out = ve.probe(ve.media_abs(job["out_rel"]))
    assert (out["width"], out["height"], out["has_audio"]) == (1920, 1080, False)
    same = ve.start_render("src.mp4", {"format": "original"})
    assert same["noop"] and same["out_rel"] == "src.mp4"


@needs_ffmpeg
def test_editor_endpoints(client, clip):
    r = client.post("/api/video/edit", json={"src": "src.mp4", "spec": {"format": "square", "trim_end": 2}})
    assert r.status_code == 200
    job = r.json()
    t0 = time.time()
    while job["status"] in ("queued", "running") and time.time() - t0 < 60:
        time.sleep(0.2)
        job = client.get(f"/api/video/edit/{job['id']}").json()
    assert job["status"] == "done", job.get("error")
    info = client.get("/api/video/probe", params={"rel": job["out_rel"]}).json()
    assert (info["width"], info["height"]) == (1080, 1080)
    assert info["edit"]["src"] == "src.mp4" and info["source"]["width"] == 640
    assert client.get("/api/video/probe", params={"rel": "../.settings.json"}).status_code == 400
    assert client.post("/api/video/edit", json={"src": "nope.mp4"}).status_code == 400
    assert client.get("/api/video/edit/unknown").status_code == 404
    fit = client.get("/api/video/fit", params={"rel": job["out_rel"], "platforms": "discord,x"}).json()
    assert [r["level"] for r in fit["rows"]] == ["ok", "ok"]


# ── the forms ───────────────────────────────────────────────────────────────────────────

@pytest.fixture
def no_draft(monkeypatch, tmp_path):
    from app.services import draft_store
    monkeypatch.setattr(draft_store, "DRAFT_PATH", str(tmp_path / "draft.json"))


def _jobs_of(brief_id):
    with Session(engine) as s:
        posts = {p.id: p for p in s.exec(select(Post).where(Post.brief_id == brief_id)).all()}
        out = {}
        for j in s.exec(select(Job)).all():
            pl = json.loads(j.payload_json or "{}")
            if pl.get("post_id") in posts:
                out[str(posts[pl["post_id"]].platform.value)] = pl
        return out, s.get(Brief, brief_id)


def test_post_form_sends_the_video_title_to_youtube_only(client, no_draft):
    text = f"Clip {uuid.uuid4().hex[:8]}"
    r = client.post("/create/post", follow_redirects=False, data={
        "text_ru": "Клип", "text_en": text, "platforms": ["yt", "reddit", "discord"],
        "gallery_order": "1_clip_9x16_ab.mp4", "yt_title": "Night drive — devlog"})
    assert r.status_code == 303
    jobs, brief = _jobs_of(int(r.headers["location"].rsplit("/", 1)[-1]))
    assert jobs["yt"]["yt_title"] == "Night drive — devlog" and jobs["yt"]["media_rel"] == "1_clip_9x16_ab.mp4"
    assert "yt_title" not in jobs["reddit"] and brief.title is None   # Reddit keeps its own title


def test_video_form_routes_each_format(client, no_draft):
    def submit(**media):
        r = client.post("/create/video", follow_redirects=False, data={
            "text_en": f"V {uuid.uuid4().hex[:8]}", "title": "T", "platforms": ["yt", "tiktok", "vk_video"], **media})
        return _jobs_of(int(r.headers["location"].rsplit("/", 1)[-1]))

    jobs, brief = submit(media_horizontal="h.mp4", media_vertical="v.mp4")
    assert jobs["yt"]["media_rel"] == "h.mp4" and jobs["vk_video"]["media_rel"] == "h.mp4"
    assert brief.media_vertical_path == "v.mp4"           # what the TikTok publisher reads
    jobs, brief = submit(media_vertical="v.mp4")           # no 16:9: YouTube gets the Short
    assert jobs["yt"]["media_rel"] == "v.mp4" and "tiktok" in jobs
    jobs, _ = submit(media_horizontal="h.mp4")
    assert "tiktok" not in jobs                           # TikTok needs a 9:16


def test_settings_save_music_folders(client, settings_file):
    r = client.post("/api/settings/save", data={"music_dir": ' E:\\Music \n\n"D:\\Shorts";F:\\X ',
                                                "discord_upload_limit_mb": "50"})
    assert r.status_code == 200
    saved = json.loads(settings_file.read_text(encoding="utf-8"))
    assert saved["music_dir"] == "E:\\Music\nD:\\Shorts\nF:\\X"
    assert saved["discord_upload_limit_mb"] == 50
