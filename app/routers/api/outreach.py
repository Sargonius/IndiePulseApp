"""Creator outreach (/outreach): similar games, discovery runs, contacts, letters, keys."""

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.db.session import get_session
from app.models.models import Creator, CreatorVideo, OutreachContact, OutreachGame, Project, SteamKey
from app.services import outreach as core
from app.services import outreach_discovery as disc
from app.services.settings import save_settings as save_app_settings

router = APIRouter()


def _page(project_id: int, msg: str = "", anchor: str = "") -> RedirectResponse:
    url = f"/outreach?project={int(project_id)}" + (f"&msg={quote(msg)}" if msg else "") + (f"#{anchor}" if anchor else "")
    return RedirectResponse(url, status_code=303)


def _project(session: Session, project_id: int) -> Project:
    p = session.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return p


def _contact(session: Session, contact_id: int) -> OutreachContact:
    c = session.get(OutreachContact, contact_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Contact not found")
    return c


def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "error": msg}, status_code=status)


# ── Similar games ────────────────────────────────────────────────────────────
@router.post("/outreach/{project_id}/games")
def game_add(project_id: int, text: str = Form(""), session: Session = Depends(get_session)):
    _project(session, project_id)
    try:
        g = disc.add_game(session, project_id, text)
    except ValueError as exc:
        return _err(str(exc))
    except Exception as exc:
        return _err(f"Steam didn’t answer ({type(exc).__name__}) — try again or add the game by name.")
    return {"ok": True, "id": g.id, "name": g.name}


@router.post("/outreach/games/{game_id}")
def game_update(game_id: int, status: str = Form(""), search_query: Optional[str] = Form(None),
                session: Session = Depends(get_session)):
    g = session.get(OutreachGame, game_id)
    if g is None:
        raise HTTPException(status_code=404, detail="Game not found")
    if status:
        if status not in ("active", "suggested", "rejected"):
            return _err("unknown status")
        g.status = status
    if search_query is not None:
        q = search_query.strip()[:200]
        if q != (g.search_query or ""):
            g.search_query = q or None
            g.searched_at = None  # a new query deserves a new search
    session.add(g)
    session.commit()
    if status:
        core.rescore(session, g.project_id)
    return {"ok": True, "status": g.status, "search_query": g.search_query or ""}


@router.post("/outreach/{project_id}/games/bulk")
def games_bulk(project_id: int, action: str = Form(...), top: int = Form(10),
               session: Session = Depends(get_session)):
    _project(session, project_id)
    games = session.exec(select(OutreachGame).where(OutreachGame.project_id == project_id)).all()
    suggested = sorted([g for g in games if g.status == "suggested"],
                       key=lambda g: -(g.similarity if g.similarity is not None else 0.5))
    if action == "use_top":
        for g in suggested[:max(1, min(50, top))]:
            g.status = "active"
            session.add(g)
    elif action == "reject_suggested":
        for g in suggested:
            g.status = "rejected"
            session.add(g)
    else:
        return _err("unknown action")
    session.commit()
    core.rescore(session, project_id)
    return {"ok": True}


# ── Background runs ──────────────────────────────────────────────────────────
@router.post("/outreach/{project_id}/run")
def run(project_id: int, kind: str = Form(...), force: int = Form(0), session: Session = Depends(get_session)):
    _project(session, project_id)
    kwargs = {"force": bool(force)} if kind == "youtube" else {"only_new": not force} if kind == "links" else {}
    ok, err = disc.start(kind, project_id, **kwargs)
    return {"ok": ok, "error": err} if ok else _err(err, 409)


@router.get("/outreach/job")
def job(session: Session = Depends(get_session)):
    return {**disc.job_status(), "search_calls": disc.search_calls_today(session),
            "search_limit": disc.SEARCH_DAILY_LIMIT}


@router.post("/outreach/yt_key")
def yt_key(project_id: int = Form(0), key: str = Form("")):
    save_app_settings({"yt_api_key": key.strip()})
    return _page(project_id, "YouTube API key saved." if key.strip() else "YouTube API key removed — using the connected channel.", "settings")


# ── Creators ─────────────────────────────────────────────────────────────────
@router.post("/outreach/{project_id}/creators")
def creator_add(project_id: int, url: str = Form(""), name: str = Form(""), email: str = Form(""),
                notes: str = Form(""), session: Session = Depends(get_session)):
    _project(session, project_id)
    try:
        creator = disc.add_creator_from_url(session, project_id, url, name, email, notes)
    except (ValueError, disc.YTError) as exc:
        return _page(project_id, f"Not added: {exc}", "add")
    return _page(project_id, f"Added {creator.name} to the shortlist.", "add")


@router.post("/outreach/{project_id}/import")
async def creators_import(project_id: int, file: UploadFile = File(...), session: Session = Depends(get_session)):
    _project(session, project_id)
    raw = await file.read()
    if len(raw) > 5_000_000:
        return _page(project_id, "Not imported: the file is over 5 MB.", "add")
    text = raw.decode("utf-8-sig", errors="replace")
    try:
        res = core.import_csv(session, project_id, text, file.filename or "")
    except ValueError as exc:
        return _page(project_id, f"Not imported: {exc}.", "add")
    return _page(project_id, f"CSV: {res['added']} new creators, {res['updated']} updated, {res['skipped']} rows skipped.", "add")


@router.post("/outreach/creators/{creator_id}")
def creator_update(creator_id: int, ignored: Optional[int] = Form(None), do_not_contact: Optional[int] = Form(None),
                   add_email: str = Form(""), notes: Optional[str] = Form(None), project_id: int = Form(0),
                   session: Session = Depends(get_session)):
    c = session.get(Creator, creator_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Creator not found")
    if ignored is not None:
        c.ignored = bool(ignored)
    if do_not_contact is not None:
        c.do_not_contact = bool(do_not_contact)
    if add_email.strip():
        emails = core.extract_emails(add_email) or []
        if not emails:
            return _err("that doesn’t look like an email address")
        core.add_emails(c, emails, "manual")
    if notes is not None:
        c.notes = notes.strip()[:2000] or None
    session.add(c)
    session.commit()
    if project_id:
        core.rescore(session, project_id)
    return {"ok": True}


@router.post("/outreach/creators/{creator_id}/scan")
def creator_scan(creator_id: int, project_id: int = Form(0), session: Session = Depends(get_session)):
    import httpx
    c = session.get(Creator, creator_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Creator not found")
    targets = disc.scan_targets(core.links_of(c))
    if not targets:
        return {"ok": True, "found": 0, "checked": [], "message": "No linked sites to check."}
    with httpx.Client(timeout=10, follow_redirects=True, headers=disc.UA) as client:
        res = disc.scan_creator_links(client, core.links_of(c))
    found = sum(core.add_emails(c, [e], f"link:{h}") for e, h in res)
    c.links_scanned_at = core.utc_now()
    session.add(c)
    session.commit()
    if project_id:
        core.rescore(session, project_id)
    return {"ok": True, "found": found, "checked": [core.host_of(u) for u in targets]}


# ── Contacts ─────────────────────────────────────────────────────────────────
def _dt(value) -> Optional[str]:
    return value.strftime("%b %d, %Y") if value else None


def contact_detail(session: Session, contact: OutreachContact) -> dict:
    cr = session.get(Creator, contact.creator_id)
    cfg = core.load_settings(session, contact.project_id)
    shared = core.shared_emails(session)
    vids = core.project_videos(session, contact.project_id).get(cr.id, [])
    row = core.contact_row(contact, cr, vids, shared, core.csv_codes(cfg["languages"]) or ["en"])
    key = session.get(SteamKey, contact.key_id) if contact.key_id else None
    all_vids = session.exec(select(CreatorVideo).where(CreatorVideo.creator_id == cr.id)).all()
    games = {g.id: g for g in session.exec(select(OutreachGame).where(
        OutreachGame.id.in_([v.game_id for v in all_vids] or [0]))).all()}
    videos = [{"game": games[v.game_id].name if v.game_id in games else "?", "title": v.title,
               "url": f"https://www.youtube.com/watch?v={v.video_id}", "views": v.views,
               "published": _dt(v.published_at),
               "this_project": v.game_id in games and games[v.game_id].project_id == contact.project_id
               and games[v.game_id].status == "active"}
              for v in sorted(all_vids, key=lambda v: v.published_at or core.datetime.min, reverse=True)][:20]
    email = row["email"]
    lang = core.letter_language(cr, cfg)
    return {
        "ok": True, "id": contact.id, "status": contact.status, "score": row["score"],
        "score_parts": row["score_parts"], "email": email,
        "emails": [{**e, "shared": e["email"] in shared} for e in core.emails_of(cr)],
        "subject": contact.subject or "", "body": contact.body or "", "notes": contact.notes or "",
        "coverage_url": contact.coverage_url or "", "sent_at": _dt(contact.sent_at),
        "followup_at": _dt(contact.followup_at), "followups": contact.followups,
        "followup_due": row["followup_due"], "replied_at": _dt(contact.replied_at),
        "key": {"key": key.key, "status": key.status, "batch": key.batch} if key else None,
        "letter_language": core.LANG_NAMES.get(lang, lang),
        "gmail_url": core.gmail_compose_url(email, contact.subject, contact.body) if email and contact.body else "",
        "creator": {
            "id": cr.id, "platform": cr.platform, "name": cr.name, "handle": cr.handle, "url": cr.url,
            "about_url": (cr.url.rstrip("/") + "/about") if cr.platform == "youtube" and cr.url else "",
            "avatar_url": cr.avatar_url, "subscribers": cr.subscribers, "country": cr.country,
            "language": core.LANG_NAMES.get(cr.language, cr.language) if cr.language else None,
            "last_upload": _dt(cr.last_upload_at), "description": (cr.description or "")[:1200],
            "links": core.links_of(cr)[:15], "links_scanned": _dt(cr.links_scanned_at),
            "ignored": cr.ignored, "do_not_contact": cr.do_not_contact, "notes": cr.notes or "",
        },
        "videos": videos,
    }


@router.get("/outreach/contacts/{contact_id}")
def contact_get(contact_id: int, session: Session = Depends(get_session)):
    return contact_detail(session, _contact(session, contact_id))


@router.post("/outreach/contacts/{contact_id}")
def contact_update(contact_id: int, status: str = Form(""), email: Optional[str] = Form(None),
                   subject: Optional[str] = Form(None), body: Optional[str] = Form(None),
                   notes: Optional[str] = Form(None), coverage_url: str = Form(""),
                   session: Session = Depends(get_session)):
    c = _contact(session, contact_id)
    if email is not None:
        e = email.strip()
        if e and not core.extract_emails(e):
            return _err("that doesn’t look like an email address")
        c.email = (core.extract_emails(e) or [None])[0]
    if subject is not None:
        c.subject = subject.strip()[:300] or None
    if body is not None:
        c.body = body.strip() or None
    if notes is not None:
        c.notes = notes.strip()[:2000] or None
    session.add(c)
    session.commit()
    if status:
        creator = session.get(Creator, c.creator_id)
        if status == "sent" and creator.do_not_contact:
            return _err("this creator asked not to be contacted")
        try:
            core.set_status(session, c, status, coverage_url=coverage_url)
        except ValueError as exc:
            return _err(str(exc))
        core.rescore(session, c.project_id)
    return contact_detail(session, c)


@router.post("/outreach/contacts/{contact_id}/draft")
def contact_draft(contact_id: int, session: Session = Depends(get_session)):
    c = _contact(session, contact_id)
    if session.get(Creator, c.creator_id).do_not_contact:
        return _err("this creator asked not to be contacted")
    res = core.generate_letter(session, c)
    return {**contact_detail(session, c), "warnings": res["warnings"], "model": res["model"]}


@router.post("/outreach/contacts/{contact_id}/followup")
def contact_followup(contact_id: int, mark_sent: int = Form(0), session: Session = Depends(get_session)):
    c = _contact(session, contact_id)
    if mark_sent:
        c.followups += 1
        c.followup_at = None
        session.add(c)
        session.commit()
        return contact_detail(session, c)
    subject, body = core.followup_text(session, c)
    email = c.email or ""
    return {"ok": True, "subject": subject, "body": body,
            "gmail_url": core.gmail_compose_url(email, subject, body) if email else ""}


# ── Keys ─────────────────────────────────────────────────────────────────────
@router.post("/outreach/{project_id}/keys")
async def keys_import(project_id: int, text: str = Form(""), batch: str = Form(""),
                      file: Optional[UploadFile] = File(None), session: Session = Depends(get_session)):
    _project(session, project_id)
    if file is not None and file.filename:
        text += "\n" + (await file.read(2_000_000)).decode("utf-8-sig", errors="replace")
    added, known = core.import_keys(session, project_id, text, batch)
    if not added and not known:
        return _page(project_id, "No Steam keys found in that text (format XXXXX-XXXXX-XXXXX).", "keys")
    return _page(project_id, f"Keys: {added} added" + (f", {known} were already in the pool" if known else "") + ".", "keys")


@router.post("/outreach/keys/{key_id}")
def key_update(key_id: int, status: str = Form(...), session: Session = Depends(get_session)):
    k = session.get(SteamKey, key_id)
    if k is None:
        raise HTTPException(status_code=404, detail="Key not found")
    if status not in ("activated", "void", "sent", "free"):
        return _err("unknown key status")
    if status == "free":
        if k.contact_id:
            c = session.get(OutreachContact, k.contact_id)
            if c and c.key_id == k.id:
                c.key_id = None
                session.add(c)
        k.contact_id, k.assigned_at = None, None
    k.status = status
    session.add(k)
    session.commit()
    return {"ok": True, "status": k.status}


# ── Settings ─────────────────────────────────────────────────────────────────
@router.post("/outreach/{project_id}/settings")
def settings_save(project_id: int, pitch: str = Form(""), links: str = Form(""), signature: str = Form(""),
                  languages: str = Form(""), letter_languages: str = Form(""), include_key: Optional[str] = Form(None),
                  followup_days: int = Form(7), years: int = Form(3), pages: int = Form(2),
                  campaign: str = Form("outreach"), session: Session = Depends(get_session)):
    _project(session, project_id)
    core.save_settings(session, project_id, {
        "pitch": pitch, "links": links, "signature": signature,
        "languages": ",".join(core.csv_codes(languages)) or "en",
        "letter_languages": ",".join(core.csv_codes(letter_languages)) or "en",
        "include_key": include_key == "1", "followup_days": followup_days, "years": years, "pages": pages,
        "campaign": campaign or "outreach"})
    core.rescore(session, project_id)
    return _page(project_id, "Letter settings saved.", "settings")
