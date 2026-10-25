"""/tiktok pipeline: TikTok version of a Short, sending, going live, numbers, checklist."""
import json
import re
from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app.models.models import Brief, Job, Post, PostStatusEnum, TikTokItem
from app.services import tiktok_pipeline as tp
from app.services import tiktok_regions, video_edit

CONNECTED = {"tiktok_client_key": "k", "tiktok_client_secret": "s", "tiktok_refresh_token": "r",
             "tiktok_access_token": "a", "tiktok_token_expires_at": 9_999_999_999,
             "tiktok_scope": "user.info.basic,video.upload,video.list,user.info.stats,user.info.profile",
             "tiktok_open_id": "acct1"}
VID = str((1_760_000_000 << 32) + 12345)   # a video id posted at unix 1760000000 (2025-10-09 08:53 UTC)
JP, US, DE = (tiktok_regions.get(c) for c in ("JP", "US", "DE"))


@pytest.fixture(autouse=True)
def isolated(session, tmp_path, monkeypatch):
    """Own media/ and sidecar folders; items of earlier tests out of the way (one DB per run)."""
    media = tmp_path / "media"
    media.mkdir()
    monkeypatch.setattr(video_edit, "MEDIA_DIR", str(media))
    monkeypatch.setattr(video_edit, "EDITS_DIR", str(tmp_path / "edits"))
    for it in session.exec(select(TikTokItem)).all():
        it.archived = True
        session.add(it)
    session.commit()
    monkeypatch.setattr(tp, "file_info", lambda rel: {"duration": 18.0, "width": 1080, "height": 1920,
                                                      "has_audio": True} if rel else {})
    return media


@pytest.fixture
def renders(monkeypatch):
    calls = []

    def fake(src, spec, music_ok=None):
        calls.append((src, spec))
        return {"id": f"job{len(calls)}", "status": "queued", "out_rel": f"tt_{len(calls)}_9x16.mp4", "spec": spec}
    monkeypatch.setattr(video_edit, "start_render", fake)
    return calls


@pytest.fixture
def scheduled(monkeypatch):
    from app.jobs import tasks
    calls = []
    monkeypatch.setattr(tasks.publish_news_post, "schedule", lambda args, delay=0: calls.append(args))
    return calls


@pytest.fixture
def no_translate(monkeypatch):
    calls = []

    def fake(fields, target):   # another language: the caption marked, the hashtags as they were
        calls.append((dict(fields), target))
        tag = {"ja": "#インディーゲーム #ゲーム開発 #Steam", "en": "#indiegame", "ru": "#инди"}.get(target, fields.get("tags"))
        desc = {"ja": "護衛ミッション", "en": "Escort mission", "ru": "Конвой"}.get(target, f"[{target}] Escort mission")
        return {"title": "", "desc": desc if fields.get("desc") else "", "tags": tag if fields.get("tags") else "",
                "provider": "test"}
    monkeypatch.setattr(tp.repost, "translate", fake)
    return calls


@pytest.fixture
def short(session, isolated):
    def _make(title="Escorting the convoy", text_en="Wasteland Courier — wishlist on Steam: {steam}",
              tags="#Shorts #indiegame #gamedev", spec=None, music_path=None, **kw):
        (isolated / "src.mp4").write_bytes(b"src")
        (isolated / "src_vertical_ab12.mp4").write_bytes(b"vertical")
        b = Brief(user_id=1, type="video", title=title, text_en=text_en, tags_json=tags, vertical_916=True,
                  media_path="src.mp4", media_vertical_path="src_vertical_ab12.mp4", music_path=music_path,
                  edit_spec_json=json.dumps(spec) if spec is not None else None, **kw)
        session.add(b)
        session.commit()
        session.refresh(b)
        return b
    return _make


def _item(session, brief, **kw):
    it = TikTokItem(brief_id=brief.id, lang=kw.pop("lang", "ja"), caption=kw.pop("caption", "護衛ミッション"),
                    tags=kw.pop("tags", "#インディーゲーム #ゲーム開発 #Steam"), video_rel=kw.pop("video_rel", "src_vertical_ab12.mp4"),
                    **kw)
    session.add(it)
    session.commit()
    session.refresh(it)
    return it


class FakeClient:
    def __init__(self, status=None, videos=None):
        self.status = status or {"status": "SEND_TO_USER_INBOX"}
        self.videos = videos or []
        self.queried = []

    def check_status(self, publish_id):
        return self.status

    def query_videos(self, ids, fields):
        self.queried.append(list(ids))
        return [v for v in self.videos if v["id"] in ids]

    def user_info(self, fields):
        return {"open_id": "acct1", "display_name": "Sargonius", "username": "sargonius", "follower_count": 12}


# ── texts ────────────────────────────────────────────────────────────────────

def test_caption_drops_links_and_the_colon_they_leave():
    assert tp.clean_caption("Wasteland Courier — wishlist on Steam: {steam}") == "Wasteland Courier — wishlist on Steam"
    assert tp.clean_caption("Look\nhttps://store.steampowered.com/app/1\nmore") == "Look\n\nmore"


def test_tags_lose_youtube_only_ones_and_stop_at_five():
    assert tp.clean_tags("#Shorts #a, b #c #d #e #f #g") == "#a #b #c #d #e"


def test_video_id_carries_its_post_time():
    assert tp.id_time(VID) == datetime.utcfromtimestamp(1_760_000_000)
    assert tp.id_time("123") is None and tp.id_time("x") is None


@pytest.mark.parametrize("url,vid", [
    (f"https://www.tiktok.com/@sargonius/video/{VID}?is_from_webapp=1", VID),
    (f"https://m.tiktok.com/v/{VID}.html", VID),
    (VID, VID),
])
def test_links_give_the_video_id(url, vid):
    assert tp.parse_link(url)[0] == vid


def test_a_non_tiktok_link_is_refused():
    with pytest.raises(tp.PipelineError):
        tp.parse_link("https://youtube.com/shorts/abc")


# ── the TikTok version of the video ──────────────────────────────────────────

def test_short_without_music_keeps_its_render_and_gets_a_sidecar(session, short, renders, no_translate):
    b = short(spec={"crop_x": 0.4, "overlays": [{"text": "護衛ミッション", "pos": "top", "size": "m",
                                                 "start": 0, "end": 3}]})
    item, notes = tp.create_from_brief(session, b, "ja")
    assert item.video_rel == "src_vertical_ab12.mp4" and not renders
    edit = video_edit.load_edit("src_vertical_ab12.mp4")
    assert edit["src"] == "src.mp4" and edit["spec"]["crop_x"] == 0.4 and edit["spec"]["music"] is None
    # the caption was English → translated; the frame text already Japanese → untouched
    assert item.caption == "護衛ミッション" and "#Shorts" not in item.tags and not notes


def test_baked_music_is_rendered_off(session, short, renders, no_translate):
    b = short(spec={"music": {"path": "E:/Music/track.mp3"}, "overlays": []})
    item, _ = tp.create_from_brief(session, b, "ja")
    (src, spec), = renders
    assert src == "src.mp4" and spec["music"] is None and spec["format"] == "vertical"
    assert item.render_id == "job1" and item.video_rel == "tt_1_9x16.mp4"
    assert tp.stage(item, None) == "rendering"


def test_english_frame_text_is_translated_into_the_render(session, short, renders, no_translate):
    b = short(spec={"overlays": [{"text": "Escorting the convoy", "pos": "top", "size": "m", "start": 0, "end": 3}]})
    tp.create_from_brief(session, b, "ja")
    (_, spec), = renders
    assert spec["overlays"][0]["text"] == "護衛ミッション"


def test_legacy_short_is_rebuilt_from_its_columns(session, short, renders, no_translate):
    b = short(music_path="E:/Music/old.mp3", crop_x=30, crop_scale=1.2, trim_start=1.5)
    tp.create_from_brief(session, b, "ja")
    (_, spec), = renders
    assert spec["crop_x"] == 0.3 and spec["zoom"] == 1.2 and spec["trim_start"] == 1.5 and spec["music"] is None


def test_a_short_already_in_the_pipeline_comes_back(session, short, renders, no_translate):
    b = short(spec={})
    first, _ = tp.create_from_brief(session, b, "ja")
    again, _ = tp.create_from_brief(session, b, "en")
    assert again.id == first.id


def test_render_states(session, short, monkeypatch):
    b = short(spec={})
    item = _item(session, b, video_rel="tt_new.mp4", render_id="j1")
    monkeypatch.setattr(video_edit, "get_job", lambda jid: {"status": "error", "error": "ffmpeg exit 1"})
    assert tp._check_render(item) and tp.stage(item, None) == "render_failed" and "ffmpeg" in item.render_error
    item.render_id, item.render_error = "j2", None
    monkeypatch.setattr(video_edit, "get_job", lambda jid: None)  # the server restarted mid-render
    assert tp._check_render(item) and "Render again" in item.render_error


# ── sending, live, numbers ───────────────────────────────────────────────────

def test_send_queues_the_tiktok_file_past_the_duplicate_guard(session, short, set_settings, scheduled):
    set_settings(**CONNECTED)
    item = _item(session, short(spec={}))
    from app.services.settings import load_settings
    tp.send(session, item, load_settings())
    post = session.get(Post, item.post_id)
    job_id, post_id = scheduled[0]
    payload = json.loads(session.get(Job, job_id).payload_json)
    assert post_id == post.id and post.platform.value == "tiktok" and post.status == PostStatusEnum.queued
    assert payload["media_rel"] == "src_vertical_ab12.mp4" and payload["allow_duplicate"] is True
    assert json.loads(post.overrides_json)["text_en"] == "護衛ミッション\n\n#インディーゲーム #ゲーム開発 #Steam"
    assert tp.stage(item, post) == "sending" and item.open_id == "acct1"
    with pytest.raises(tp.PipelineError) as exc:
        tp.send(session, item, CONNECTED)
    assert exc.value.code == "already_sent"


def test_send_needs_a_connection(session, short):
    item = _item(session, short(spec={}))
    with pytest.raises(tp.PipelineError) as exc:
        tp.send(session, item, {})
    assert exc.value.code == "not_connected"


def _in_inbox(session, item):
    post = Post(user_id=1, brief_id=item.brief_id, platform="tiktok", status=PostStatusEnum.awaiting_finish,
                platform_post_id="v_inbox_file~v2.1")
    session.add(post)
    session.commit()
    item.post_id, item.open_id = post.id, "acct1"
    session.add(item)
    session.commit()
    return post


def test_posted_draft_goes_live_with_id_url_time_and_numbers(session, short, set_settings, monkeypatch):
    set_settings(**CONNECTED)
    item = _item(session, short(spec={}))
    post = _in_inbox(session, item)
    fake = FakeClient(status={"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [int(VID)]},
                      videos=[{"id": VID, "view_count": 340, "like_count": 21, "comment_count": 3, "share_count": 2}])
    monkeypatch.setattr(tp, "_client", lambda cfg: fake)
    out = tp.sync(session, CONNECTED, force=True)
    session.refresh(post)
    session.refresh(item)
    assert out["live"] == 1 and out["metrics"] == 1
    assert post.status == PostStatusEnum.done and post.platform_post_id == VID and item.video_id == VID
    assert post.published_at == datetime.utcfromtimestamp(1_760_000_000)
    assert post.platform_url == f"https://www.tiktok.com/@sargonius/video/{VID}"
    m = json.loads(post.metrics_json)
    assert (m["views"], m["likes"], m["comments"], m["reposts"]) == (340, 21, 3, 2)
    hist = json.loads(item.history_json)
    assert hist[0][1:] == [0, 0, 0, 0] and hist[-1][1] == 340
    st = tp.item_state(session, item)
    assert st["stage"] == "live" and st["metrics"]["shares"] == 2 and st["history"][-1][1] == 340


def test_a_dropped_draft_marks_the_post_failed(session, short, monkeypatch):
    item = _item(session, short(spec={}))
    post = _in_inbox(session, item)
    monkeypatch.setattr(tp, "_client", lambda cfg: FakeClient(status={"status": "FAILED",
                                                                      "fail_reason": "publish_cancelled"}))
    tp.sync(session, CONNECTED, force=True)
    session.refresh(post)
    assert post.status == PostStatusEnum.error and "publish_cancelled" in post.error_text
    assert tp.stage(item, post) == "send_failed"


def test_status_checks_are_spaced_out(session, short, monkeypatch):
    item = _item(session, short(spec={}))
    _in_inbox(session, item)
    item.status_checked_at = datetime.utcnow() - timedelta(minutes=3)
    session.add(item)
    session.commit()
    monkeypatch.setattr(tp, "_client", lambda cfg: FakeClient())
    assert tp.sync(session, CONNECTED, in_worker=True)["statuses"] == 0   # tick: 8 min gap
    assert tp.sync(session, CONNECTED, force=True)["statuses"] == 1       # Refresh: 45 s gap


def test_no_numbers_without_the_video_list_scope(session, short, monkeypatch):
    cfg = dict(CONNECTED, tiktok_scope="user.info.basic,video.upload")
    item = _item(session, short(spec={}), video_id=VID)
    post = Post(user_id=1, brief_id=item.brief_id, platform="tiktok", status=PostStatusEnum.done,
                platform_post_id=VID, published_at=datetime.utcnow() - timedelta(hours=1))
    session.add(post)
    session.commit()
    item.post_id = post.id
    session.add(item)
    session.commit()
    fake = FakeClient()
    monkeypatch.setattr(tp, "_client", lambda cfg: fake)
    assert tp.sync(session, cfg, force=True)["metrics"] == 0 and not fake.queried


def test_link_by_hand_makes_it_live(session, short):
    item = _item(session, short(spec={}))
    tp.link(session, item, f"https://www.tiktok.com/@me/video/{VID}?lang=en")
    post = session.get(Post, item.post_id)
    assert post.status == PostStatusEnum.done and post.platform_url == f"https://www.tiktok.com/@me/video/{VID}"
    other = _item(session, short(spec={}))
    with pytest.raises(tp.PipelineError) as exc:
        tp.link(session, other, VID)
    assert exc.value.code == "linked_elsewhere"


def test_factory_drafts_after_the_connect_are_adopted(session, short):
    b = short(spec={})
    cfg = dict(CONNECTED, tiktok_connected_at=int((datetime.utcnow() - timedelta(hours=1)).timestamp()))
    tp.adopt_factory_posts(session, cfg)  # TikTok posts other tests left in the shared DB
    old = Post(user_id=1, brief_id=b.id, platform="tiktok", status=PostStatusEnum.awaiting_finish,
               created_at=datetime(2026, 7, 11))
    new = Post(user_id=1, brief_id=b.id, platform="tiktok", status=PostStatusEnum.awaiting_finish,
               platform_post_id="v_inbox_file~v2.9", created_at=datetime.utcnow())
    session.add(old)
    session.add(new)
    session.commit()
    assert tp.adopt_factory_posts(session, cfg) == 1
    adopted = session.exec(select(TikTokItem).where(TikTokItem.post_id == new.id)).one()
    assert adopted.source == "factory"
    assert session.exec(select(TikTokItem).where(TikTokItem.post_id == old.id)).first() is None
    assert tp.adopt_factory_posts(session, cfg) == 0


# ── checklist ────────────────────────────────────────────────────────────────

def _rows(item, post=None, spec=None, info=None, region=None, langs=()):
    return {r["key"]: r for r in tp.checks(item, post, info or {"duration": 18, "has_audio": True}, spec, region,
                                           langs)}


def _live(item, when):
    return Post(user_id=1, brief_id=item.brief_id, platform="tiktok", status=PostStatusEnum.done, published_at=when)


def test_checks_read_the_clip(session, short):
    item = _item(session, short(spec={}), caption="Escort mission https://x.y", tags="#indiegame")
    rows = _rows(item, spec={"overlays": [{"text": "Escort!"}], "music": {"path": "m.mp3"}}, info={"duration": 40})
    assert rows["screen_text"]["state"] == "warn" and rows["screen_text"]["action"] == "screen_text"
    assert rows["no_music"]["action"] == "drop_music"
    assert rows["length"]["state"] == "warn" and rows["caption"]["state"] == "warn"
    assert rows["tags"]["state"] == "warn" and rows["geo"]["state"] == "na"
    ok = _rows(_item(session, short(spec={})), spec={"overlays": [{"text": "護衛ミッション"}], "music": None})
    assert all(ok[k]["state"] == "ok" for k in ("screen_text", "no_music", "length", "caption", "tags"))


def test_a_tick_turns_a_check_green_and_post_time_is_judged_in_japan(session, short):
    item = _item(session, short(spec={}), checks_json=json.dumps({"sound": True, "length": True}))
    post = _live(item, datetime(2026, 10, 11, 12, 30))   # 21:30 in Japan
    rows = _rows(item, post, info={"duration": 40}, region=JP)
    assert rows["sound"]["state"] == "ok" and rows["length"]["state"] == "ok"
    assert rows["posted_time"]["state"] == "ok" and rows["posted_time"]["note"] == "≈21:30 in Japan"
    assert rows["posted_time"]["label"] == "Posted 19:00–23:00 in Japan"
    assert rows["geo"]["label"] == "Japan share typed in, after 2–3 days" and "region Japan" in rows["sound"]["hint"]
    post.published_at = datetime(2026, 10, 11, 1, 0)             # 10:00 in Japan
    assert _rows(item, post, region=JP)["posted_time"]["state"] == "warn"


def test_a_us_clip_is_judged_in_us_eastern_time_summer_and_winter(session, short):
    item = _item(session, short(spec={}), lang="en", caption="Escort mission", tags="#indiegame #gamedev #Steam",
                 video_id=VID)
    post = _live(item, datetime(2026, 7, 15, 23, 30))    # 19:30 EDT (UTC−4): the evening
    row = _rows(item, post, region=US)["posted_time"]
    assert (row["state"], row["note"], row["label"]) == ("ok", "19:30 US Eastern time",
                                                         "Posted 19:00–23:00 US Eastern time")
    post.published_at = datetime(2027, 1, 15, 23, 30)    # the same UTC hour is 18:30 EST (UTC−5): too early
    row = _rows(item, post, region=US)["posted_time"]
    assert (row["state"], row["note"]) == ("warn", "18:30 US Eastern time")
    assert _rows(item, post, region=US)["geo"]["label"] == "US share typed in, after 2–3 days"
    # a clip in another language than the region's: its top country
    assert _rows(item, post, region=JP)["geo"]["label"] == tp.GEO_ELSEWHERE


def test_without_a_region_the_checklist_names_no_country(session, short):
    for lang in ("ja", "en"):
        item = _item(session, short(spec={}), lang=lang)
        rows = _rows(item, _live(item, datetime(2026, 10, 11, 12, 30)))
        assert "posted_time" not in rows and rows["geo"]["label"] == tp.GEO_ELSEWHERE
        assert "target region" in rows["sound"]["hint"]
        assert not re.search(r"\bJapan\b", json.dumps(list(rows.values()), ensure_ascii=False))


def test_letters_two_languages_share_are_a_check_it_yourself_not_a_warning(session, short):
    langs = ["de", "ru", "en"]   # Germany's language first, then the languages of Settings
    item = _item(session, short(spec={}), lang="de", caption="Eskortiere den Konvoi", tags="#indiegame #gamedev #Steam")
    rows = _rows(item, spec={"overlays": [{"text": "Escort the convoy"}], "music": None}, region=DE, langs=langs)
    assert rows["caption"]["state"] == "todo" and "German or English" in rows["caption"]["note"]
    assert "Reads as" not in rows["caption"]["note"]
    assert rows["screen_text"]["state"] == "todo" and rows["screen_text"]["action"] == "screen_text"
    assert rows["tags"]["state"] == "ok"
    assert _rows(item, region=DE, langs=["de", "ru"])["caption"]["state"] == "ok"   # no other Latin language
    item.caption = "Сопроводи конвой"
    row = _rows(item, region=DE, langs=langs)["caption"]
    assert (row["state"], row["note"]) == ("warn", "Reads as Russian")


def test_a_language_with_letters_of_its_own_needs_a_hashtag_in_them(session, short):
    langs = ["ko", "ru", "en"]
    item = _item(session, short(spec={}), lang="ko", caption="호송 임무 · Steam", tags="#indiegame #gamedev #Steam")
    rows = _rows(item, langs=langs)
    assert rows["caption"]["state"] == "ok"                       # Hangul wins over the Latin “Steam”
    assert (rows["tags"]["state"], rows["tags"]["note"]) == ("warn", "None in Korean")
    item.tags = "#인디게임 #gamedev #Steam"
    assert _rows(item, langs=langs)["tags"]["state"] == "ok"
    # Taiwan: kana reads as Japanese; Chinese characters alone could be either, with Japanese in play
    tw = _item(session, short(spec={}), lang="zh-hant", caption="護衛ミッション", tags="#獨立遊戲 #遊戲開發 #Steam")
    assert _rows(tw, langs=["zh-hant", "ja", "en"])["caption"]["note"] == "Reads as Japanese"
    tw.caption = "護衛任務"
    assert _rows(tw, langs=["zh-hant", "ja", "en"])["caption"]["state"] == "todo"
    assert _rows(tw, langs=["zh-hant", "en"])["caption"]["state"] == "ok"


def test_every_language_of_settings_has_its_script():
    from app.services import languages
    assert sorted(set(languages.NAMES) - set(tp.SCRIPTS)) == []
    assert tp.script("インディーゲーム Steam") == "kana" and tp.script("個人開発") == "han"
    assert tp.script("인디게임 Steam") == "hangul" and tp.script("Сопроводи Steam") == "cyr"
    assert tp.script("Steam") == "lat" and tp.script("2026 🔥") is None
    assert tp.tag_hint("de") == tp.TAG_HINTS["en"] and tp.tag_hint("ko").startswith("#인디게임")


def test_caption_languages_put_the_region_first_and_never_japan_on_their_own(set_settings):
    from app.services.settings import load_settings
    set_settings(languages=["ru", "en"])
    assert tp.caption_langs(load_settings()) == ["ru", "en"]
    set_settings(tiktok_region="JP")
    assert tp.caption_langs(load_settings()) == ["ja", "ru", "en"]
    set_settings(tiktok_region="US", languages=["en", "de"])
    assert tp.caption_langs(load_settings()) == ["en", "de"]
    assert tp.known_lang("PT_BR") == "pt-br" and tp.known_lang("th") == "th" and tp.known_lang("xx") is None


def test_new_clips_take_the_region_language(session, short, renders, no_translate, set_settings, isolated):
    set_settings(languages=["ru", "en"], tiktok_region="DE")
    item, _ = tp.create_from_brief(session, short(spec={}), "")
    assert item.lang == "de" and item.caption == "[de] Escort mission"
    set_settings(tiktok_region="")
    item, _ = tp.create_from_brief(session, short(spec={}), "nonsense")
    assert item.lang == "ru"                                       # not Japanese: the first language of Settings
    (isolated / "short_0123456789ab_clip.mp4").write_bytes(b"vertical")
    up = tp.create_from_upload(session, "short_0123456789ab_clip.mp4", None, "th")
    assert up.lang == "th" and up.tags == tp.TAG_HINTS["en"]       # no Thai set: the English one
    tp.set_lang(session, up, "ko")
    assert up.lang == "ko" and up.tags == tp.TAG_HINTS["ko"]       # the starter set follows the language
    with pytest.raises(tp.PipelineError):
        tp.set_lang(session, up, "xx")


def test_update_validates_the_share(session, short):
    item = _item(session, short(spec={}))
    tp.update(session, item, {"region_share": "41.25", "checks": {"cover": True, "bogus": True}})
    assert item.jp_share == 41.2 and json.loads(item.checks_json) == {"cover": True}
    with pytest.raises(tp.PipelineError):
        tp.update(session, item, {"region_share": "lots"})
    tp.update(session, item, {"jp_share": 12})                     # its old name, from a page loaded before
    assert item.jp_share == 12.0


def test_summary_waits_for_enough_shares():
    live = [{"stage": "live", "lang": lang, "region_share": s, "metrics": {"views": v},
             "post": {"published_iso": "2026-10-11T12:00:00Z"}} for lang, s, v in (("ja", 40, 100), ("ja", 35, 300),
                                                                                    ("en", 90, 200))]
    followers = {"days": ["2026-10-10", "2026-10-12"], "values": [5, 9]}
    s = tp.summary(live, followers, JP)
    assert s["share_avg"] == 37.5 and s["share_n"] == 2 and s["views_median"] == 200 and s["followers_gained"] == 4
    assert tp.summary(live, followers, US)["share_avg"] == 90.0    # the US: the English clip's share
    s = tp.summary(live, followers)                                # no region: no share to sum up
    assert s["share_avg"] is None and s["share_n"] == 0


# ── publisher, audience, page ────────────────────────────────────────────────

def test_tiktok_publisher_uploads_the_jobs_file(session, isolated, make_post, set_settings, monkeypatch):
    from app.platforms import tiktok as tiktok_platform
    from app.services import tiktok_client, video_editor
    set_settings(**CONNECTED)
    post_id, job_id = make_post("tiktok", payload={"media_rel": "tt_only.mp4"}, media_vertical_path="other.mp4")
    sent = []
    monkeypatch.setattr(tiktok_client.TikTokClient, "upload_to_inbox",
                        lambda self, path, title="": (sent.append(path), ("pub1", None))[1])
    monkeypatch.setattr(video_editor, "ensure_video_size_limit", lambda path, plat, cfg: path)
    post, job = session.get(Post, post_id), session.get(Job, job_id)
    assert tiktok_platform.publish(session, post, job) == "ok"
    assert sent[0].replace("\\", "/").endswith("media/tt_only.mp4") and post.platform_post_id == "pub1"


def test_tiktok_followers_need_the_stats_scope():
    from app.services import audience
    ch = next(c for c in audience.CHANNELS if c.key == "tiktok")
    assert ch.ready(CONNECTED) and not ch.ready(dict(CONNECTED, tiktok_scope="user.info.basic,video.upload"))


def test_connect_asks_for_stats_scopes_only_when_ticked():
    assert tp.auth_scopes({}) == "user.info.basic,video.upload"
    assert "video.list" in tp.auth_scopes({"tiktok_want_stats": True})


def test_page_and_state_render(client, session, short, set_settings):
    set_settings(**CONNECTED)
    _item(session, short(spec={}))
    r = client.get("/tiktok")
    assert r.status_code == 200 and "TT_CTX" in r.text
    j = client.get("/api/tiktok/state").json()
    assert j["connection"]["views"] and any(i["stage"] == "ready" for i in j["items"])


def _side(client) -> str:
    """The side panel of /tiktok after its region select (the select lists every country)."""
    page = client.get("/tiktok").text
    return page.split('<aside class="tt-side">', 1)[1].split("</aside>", 1)[0]


def test_the_page_names_no_country_until_a_region_is_picked(client, session, short, set_settings):
    from app.services.settings import load_settings
    set_settings(**CONNECTED, languages=["ru", "en"])
    _item(session, short(spec={}))
    ctx = tp.page_context(session, load_settings())
    assert ctx["region"] is None and [x["key"] for x in ctx["langs"]] == ["ru", "en"]
    assert ctx["summary"]["share_avg"] is None and len(ctx["regions"]) == len(tiktok_regions.REGIONS)
    side = _side(client)
    assert '<option value="" selected>' in side
    assert "Japan" not in side.split("</select>", 1)[1] and "the target region" in side
    # picked on the page
    assert client.post("/api/tiktok/settings", json={"region": "jp"}).status_code == 200
    assert load_settings()["tiktok_region"] == "JP"
    ctx = tp.page_context(session, load_settings())
    assert ctx["region"]["clock"] == "Time in Japan" and ctx["langs"][0]["key"] == "ja"
    side = _side(client)
    assert '<option value="JP" selected>' in side and "Does a Japanese caption bring viewers from Japan" in side
    assert "Type the share of viewers from Japan" in side
    assert client.post("/api/tiktok/settings", json={"region": "XX"}).status_code == 400
    assert load_settings()["tiktok_region"] == "JP"
    client.post("/api/tiktok/settings", json={"region": ""})
    assert load_settings()["tiktok_region"] == ""
