"""Goals, manual marketing actions/metrics, the metrics refresh (home screen) and UTM links."""

from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlmodel import Session, select

from app.db.session import get_session
from app.models.models import Activity, Brief, Goal, GoalLink, Project, UtmLink, now_ts
from app.routers.api.common import _e
from app.services import marketing as mk

router = APIRouter()


def _back(next_url: str, fallback: str) -> RedirectResponse:
    """Redirect to a local page only (never to another site)."""
    url = (next_url or "").strip()
    if not url.startswith("/") or url.startswith("//") or "\\" in url:
        url = fallback
    return RedirectResponse(url, status_code=303)


def _goal_dict(g: Goal) -> dict:
    return {"id": g.id, "title": g.title, "project_id": g.project_id, "metric": g.metric,
            "metric_label": mk.metric_label(g.metric), "status": g.status}


@router.get("/goals")
def goals_list(project_id: Optional[int] = None, status: str = "active",
               session: Session = Depends(get_session)):
    """Goals for the "Goal" pickers: a project's goals plus the global ones."""
    q = select(Goal)
    if status != "all":
        q = q.where(Goal.status == status)
    rows = session.exec(q.order_by(Goal.id.desc())).all()
    if project_id is not None:
        rows = [g for g in rows if g.project_id in (0, project_id)]
    return [_goal_dict(g) for g in rows]


@router.post("/goals")
def goal_create(
    title: str = Form(""),
    project_id: str = Form("0"),
    metric: str = Form(...),
    mode: str = Form("delta"),
    target: str = Form(...),
    start_day: str = Form(""),
    deadline: str = Form(""),
    dim: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session),
):
    spec = mk.METRICS.get(metric)
    if spec is None:
        raise HTTPException(status_code=400, detail="unknown metric")
    pid = mk.parse_id(project_id) or 0
    if spec["scope"] == "global":
        pid = 0
    elif pid and session.get(Project, pid) is None:
        raise HTTPException(status_code=400, detail="project not found")
    elif not pid and spec["scope"] == "project":
        raise HTTPException(status_code=400, detail="this metric is per project — pick a project")
    try:
        target_v = float(str(target).replace(",", ".").replace(" ", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="the target must be a number")
    if target_v <= 0:
        raise HTTPException(status_code=400, detail="the target must be greater than zero")
    mode = "total" if (mode == "total" and spec["kind"] == "level") else "delta"
    try:
        start = mk.to_date(start_day).isoformat() if start_day.strip() else mk.today_str()
        end = mk.to_date(deadline).isoformat() if deadline.strip() else None
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if end and end < start:
        raise HTTPException(status_code=400, detail="the deadline is before the start")
    if not title.strip():
        num = mk.fmt_num(target_v)
        title = f"{'' if mode == 'total' else '+'}{num} — {spec['label']}"
    goal = Goal(title=title.strip()[:200], project_id=pid, metric=metric, dim=(dim or "").strip(),
                mode=mode, target=target_v, start_day=start, deadline=end, notes=(notes or "").strip() or None)
    if spec["kind"] == "level":
        base = mk.level_on_or_before(session, pid, metric, start, goal.dim)
        goal.baseline = base.value if base else None
    session.add(goal)
    session.commit()
    session.refresh(goal)
    return RedirectResponse(f"/goals/{goal.id}", status_code=303)


@router.post("/goals/{goal_id}/edit")
def goal_edit(goal_id: int, title: str = Form(""), target: str = Form(""), deadline: str = Form(""),
              notes: str = Form(""), session: Session = Depends(get_session)):
    g = session.get(Goal, goal_id)
    if g is None:
        raise HTTPException(status_code=404)
    if title.strip():
        g.title = title.strip()[:200]
    if target.strip():
        try:
            g.target = float(target.replace(",", ".").replace(" ", ""))
        except ValueError:
            raise HTTPException(status_code=400, detail="the target must be a number")
    g.deadline = mk.to_date(deadline).isoformat() if deadline.strip() else None
    g.notes = notes.strip() or None
    # a new target can "un-achieve" a goal; evaluate_goals re-marks it if still reached
    if g.status == "achieved" and not mk.goal_progress(session, g).reached:
        g.status, g.achieved_day = "active", None
    g.updated_at = now_ts()
    session.add(g)
    session.commit()
    mk.evaluate_goals(session)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@router.post("/goals/{goal_id}/status")
def goal_status(goal_id: int, status: str = Form(...), session: Session = Depends(get_session)):
    g = session.get(Goal, goal_id)
    if g is None:
        raise HTTPException(status_code=404)
    if status not in ("active", "archived"):
        raise HTTPException(status_code=400)
    g.status = status
    if status == "active":
        g.achieved_day = None
    g.updated_at = now_ts()
    session.add(g)
    session.commit()
    if status == "active":
        mk.evaluate_goals(session)
    return RedirectResponse(f"/goals/{goal_id}", status_code=303)


@router.post("/goals/{goal_id}/delete")
def goal_delete(goal_id: int, session: Session = Depends(get_session)):
    g = session.get(Goal, goal_id)
    if g is not None:
        for link in session.exec(select(GoalLink).where(GoalLink.goal_id == goal_id)).all():
            session.delete(link)
        for act in session.exec(select(Activity).where(Activity.goal_id == goal_id)).all():
            act.goal_id = None
            session.add(act)
        session.delete(g)
        session.commit()
    return RedirectResponse("/goals", status_code=303)


@router.post("/goals/{goal_id}/links")
def goal_link(goal_id: int, brief_id: str = Form(...), action: str = Form("link"),
              session: Session = Depends(get_session)):
    g = session.get(Goal, goal_id)
    bid = mk.parse_id(brief_id)
    if g is None or bid is None or session.get(Brief, bid) is None:
        raise HTTPException(status_code=404)
    link = session.exec(select(GoalLink).where(GoalLink.goal_id == goal_id, GoalLink.brief_id == bid)).first()
    if action == "unlink":
        if link is not None:
            session.delete(link)
    elif link is None:
        session.add(GoalLink(goal_id=goal_id, brief_id=bid))
    session.commit()
    return RedirectResponse(f"/goals/{goal_id}#actions", status_code=303)


@router.post("/activities")
def activity_create(title: str = Form(...), day: str = Form(""), project_id: str = Form("0"),
                    goal_id: str = Form(""), next: str = Form(""), session: Session = Depends(get_session)):
    gid = mk.parse_id(goal_id)
    goal = session.get(Goal, gid) if gid else None
    pid = mk.parse_id(project_id) or (goal.project_id if goal else 0)
    try:
        d = mk.to_date(day).isoformat() if day.strip() else mk.today_str()
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    if title.strip():
        session.add(Activity(project_id=pid, goal_id=goal.id if goal else None, day=d,
                             title=title.strip()[:300]))
        session.commit()
    return _back(next, f"/goals/{gid}#actions" if gid else "/")


@router.post("/activities/{activity_id}/delete")
def activity_delete(activity_id: int, next: str = Form(""), session: Session = Depends(get_session)):
    a = session.get(Activity, activity_id)
    if a is not None:
        session.delete(a)
        session.commit()
    return _back(next, "/")


@router.post("/metrics/manual")
def metric_manual(project_id: str = Form(...), metric: str = Form("steam_visits"), start_day: str = Form(...),
                  end_day: str = Form(...), value: str = Form(...), next: str = Form(""),
                  session: Session = Depends(get_session)):
    """A number typed in from Steamworks (e.g. store visits for a week), stored once
    on the period's last day with span_days = period length."""
    spec = mk.METRICS.get(metric) or {}
    pid = mk.parse_id(project_id)
    if not spec.get("manual") or pid is None or session.get(Project, pid) is None:
        raise HTTPException(status_code=400, detail="this metric can’t be entered manually")
    try:
        a, b = mk.to_date(start_day).isoformat(), mk.to_date(end_day).isoformat()
        v = float(str(value).replace(",", ".").replace(" ", "").replace(" ", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="check the dates and the number")
    if b < a:
        a, b = b, a
    mk.upsert_points(session, [{"project_id": pid, "metric": metric, "day": b, "value": v,
                                "span_days": mk.days_between(a, b) + 1}], source="manual")
    mk.evaluate_goals(session)
    return _back(next, "/")


@router.post("/metrics/collect", response_class=HTMLResponse)
def metrics_collect():
    """The home screen's "Refresh" button: queue a forced collection on the worker."""
    from app.jobs.tasks import collect_marketing_metrics
    collect_marketing_metrics.schedule(kwargs={"force": True}, delay=0)
    return HTMLResponse("⏳ Refresh started — data will show up in a minute, reload the page.")


@router.post("/steam/traffic/import", response_class=HTMLResponse)
async def steam_traffic_import(request: Request, session: Session = Depends(get_session)):
    """Called by /steam/traffic/import with the bookmarklet's URL fragment as the body."""
    from app.services import steam_traffic
    body = await request.body()
    if len(body) > steam_traffic.MAX_PAYLOAD_BYTES:
        return HTMLResponse("<p class='imp-err'>Too much data at once.</p>", status_code=413)
    try:
        data = steam_traffic.decode_payload(body.decode("ascii", "replace"))
    except ValueError as exc:
        return HTMLResponse(f"<p class='imp-err'>Couldn’t read the data: {_e(exc)}</p>", status_code=400)
    if not steam_traffic.token_ok(data.get("token")):
        return HTMLResponse("<p class='imp-err'>The bookmarklet is outdated or not yours — drag it again from the "
                            "<a href='/steam/traffic'>Steam traffic</a> page.</p>", status_code=403)
    res = steam_traffic.import_payload(session, data)
    if not res["projects"]:
        return HTMLResponse("<p class='imp-err'>No days matched: no project has the "
                            f"Steam App ID of the games in the CSV ({_e(', '.join(map(str, res['unknown_apps'])) or '—')}).</p>")
    items = "".join(
        f"<li><b>{_e(p['project'])}</b>: {p['days']} d ({_e(mk.fmt_day(p['first']))} — {_e(mk.fmt_day(p['last']))})"
        f" · last 7 d: visits {_e(mk.fmt_num(p['visits7']))}, impressions {_e(mk.fmt_num(p['impressions7']))}"
        + (f" · <span class='muted'>+{round(p['bot_share7'] * 100)}% bots — not counted</span>"
           if p["bot_share7"] >= 0.01 else "") + "</li>"
        for p in res["projects"])
    notes = ""
    if res["unknown_apps"]:
        notes += f"<p class='muted'>Skipped games without a project: {_e(', '.join(map(str, res['unknown_apps'])))}.</p>"
    if res["failed"]:
        notes += (f"<p class='muted'>Steam didn’t return {len(res['failed'])} d (e.g. {_e(res['failed'][0])}) — "
                  "they’ll load next time.</p>")
    return HTMLResponse(f"<p class='imp-ok'>✓ Traffic saved</p><ul>{items}</ul>{notes}")


@router.post("/steam/financial/test", response_class=HTMLResponse)
def steam_financial_test(session: Session = Depends(get_session)):
    """Settings → Steam "Check": one wishlist report for yesterday."""
    from datetime import datetime, timedelta
    from app.services import steam_partner
    from app.services.marketing_collect import steam_projects
    from app.services.settings import load_settings
    key = (load_settings().get("steam_financial_api_key") or "").strip()
    if not key:
        return HTMLResponse("<span style='color:#b91c1c'>Key not saved</span>")
    projects = steam_projects(session)
    if not projects:
        return HTMLResponse("<span style='color:#b45309'>Key saved, but no project has a Steam App ID "
                            "(🗂 Projects → Edit)</span>")
    p = projects[0]
    day = (datetime.utcnow().date() - timedelta(days=1)).isoformat()
    try:
        resp = steam_partner.wishlist_report(key, p.steam_app_id, day)
    except Exception as exc:
        return HTMLResponse(f"<span style='color:#b91c1c'>✗ {_e(exc)}</span>")
    if not resp:
        return HTMLResponse("<span style='color:#b91c1c'>✗ Empty response: the key has no access to reports "
                            f"for app {p.steam_app_id}</span>")
    s = resp.get("wishlist_summary") or {}
    return HTMLResponse(
        f"<span style='color:#15803d'>✓ {_e(p.name)}: {int(s.get('wishlist_adds') or 0)} wishlist adds "
        f"on {day}. Data since {_e(resp.get('app_min_date') or '—')}.</span>")


# ── UTM links (/utm) ─────────────────────────────────────────────────────────

def _utm_fields(session: Session, project_id, source: str, medium: str, campaign: str,
                content: str = "", base_url: str = "", need_placement: bool = True) -> dict:
    """Validate and normalize a UTM form; ValueError with a message for the user."""
    from app.services import utm
    pid = mk.parse_id(project_id)
    project = session.get(Project, pid) if pid else None
    if project is None:
        raise ValueError("pick a project")
    base = utm.check_base_url(base_url) if (base_url or "").strip() else None
    if base is None and not project.steam_app_id:
        raise ValueError("the project has no Steam App ID — enter your own link (e.g. an itch.io page)")
    if need_placement and utm.placement(source, medium) is None:
        raise ValueError("pick a network and where the link goes")
    campaign = utm.slug(campaign)
    if not campaign:
        raise ValueError("enter a campaign (Latin or Cyrillic letters), e.g. patch_oct")
    return {"project": project, "source": source, "medium": medium, "campaign": campaign,
            "content": utm.slug(content), "base_url": base}


def _utm_save(session: Session, f: dict, source: str, medium: str, note: str = "") -> UtmLink:
    """Insert or return the existing link with the same (project, source, medium, campaign, content)."""
    from sqlalchemy.exc import IntegrityError
    key = dict(project_id=f["project"].id, source=source, medium=medium, campaign=f["campaign"], content=f["content"])
    q = select(UtmLink).where(*(getattr(UtmLink, k) == v for k, v in key.items()))
    link = session.exec(q).first()
    if link is None:
        link = UtmLink(**key, base_url=f["base_url"], note=note.strip()[:300] or None)
        session.add(link)
        try:
            session.commit()
        except IntegrityError:  # saved twice at once (double click)
            session.rollback()
            return session.exec(q).one()
    elif f["base_url"] != link.base_url or (note.strip() and note.strip() != link.note):
        link.base_url = f["base_url"]
        link.note = note.strip()[:300] or link.note
        session.add(link)
        session.commit()
    session.refresh(link)
    return link


@router.get("/utm/build")
def utm_build(project_id: str = "", source: str = "", medium: str = "", campaign: str = "", content: str = "",
              base_url: str = "", session: Session = Depends(get_session)):
    """Live preview on /utm: the normalized URL exactly as it will be saved."""
    from app.services import utm
    from app.services.marketing_views import utm_link_url
    try:
        f = _utm_fields(session, project_id, source, medium, campaign, content, base_url)
    except ValueError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    link = UtmLink(project_id=f["project"].id, source=source, medium=medium, campaign=f["campaign"],
                   content=f["content"], base_url=f["base_url"])
    pl = utm.placement(source, medium)
    return {"ok": True, "url": utm_link_url(link, f["project"]), "campaign": f["campaign"],
            "content": f["content"], "hint": pl["hint"], "clickable": pl["clickable"]}


@router.post("/utm/links")
def utm_link_create(project_id: str = Form(""), source: str = Form(""), medium: str = Form(""),
                    campaign: str = Form(""), content: str = Form(""), base_url: str = Form(""),
                    note: str = Form(""), session: Session = Depends(get_session)):
    try:
        f = _utm_fields(session, project_id, source, medium, campaign, content, base_url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    link = _utm_save(session, f, source, medium, note)
    q = urlencode({"project": link.project_id, "campaign": link.campaign, "source": source, "medium": medium,
                   "saved": link.id})
    return RedirectResponse(f"/utm?{q}#utm-l{link.id}", status_code=303)


@router.post("/utm/set")
def utm_set_create(project_id: str = Form(""), campaign: str = Form(""), base_url: str = Form(""),
                   source: str = Form(""), medium: str = Form(""), session: Session = Depends(get_session)):
    """“Campaign set”: the standard profile/post links for one campaign in one click."""
    from app.services import utm
    try:
        f = _utm_fields(session, project_id, "", "", campaign, "", base_url, need_placement=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    f["content"] = ""
    for src, med in utm.CAMPAIGN_SET:
        _utm_save(session, f, src, med)
    q = urlencode({"project": f["project"].id, "campaign": f["campaign"], "set": 1,
                   "source": source, "medium": medium})
    return RedirectResponse(f"/utm?{q}#utm-set", status_code=303)


@router.post("/utm/links/{link_id}/delete")
def utm_link_delete(link_id: int, next: str = Form(""), session: Session = Depends(get_session)):
    link = session.get(UtmLink, link_id)
    if link is not None:
        session.delete(link)
        session.commit()
    return _back(next, "/utm")
