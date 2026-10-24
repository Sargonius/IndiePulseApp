"""Marketing home screen ("/") and goal pages."""

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from app.core.templating import make_templates
from sqlmodel import Session

from app.db.session import get_session
from app.models.models import Brief, Goal
from app.services import marketing as mk
from app.services import marketing_views as views
from app.services import publications as pubs
from app.services.settings import load_settings

router = APIRouter()
templates = make_templates()
templates.env.filters.update(
    num=mk.fmt_num, delta=mk.fmt_delta, day=mk.fmt_day, metric_label=mk.metric_label,
    when=pubs.fmt_when, fromjson=json.loads,
)


def _render(request: Request, name: str, ctx: dict) -> HTMLResponse:
    ctx = {"request": request, "ui_language": load_settings().get("ui_language", "en"),
           "stages": mk.PROJECT_STAGES, **ctx}
    return templates.TemplateResponse(name, ctx)


@router.get("/", response_class=HTMLResponse)
def home(request: Request, session: Session = Depends(get_session)):
    from app.services import setup
    if setup.should_show():  # a fresh install: the setup wizard until it is finished or skipped
        return RedirectResponse("/setup", status_code=303)
    return _render(request, "home.html", {"body_class": "page-index", **views.home_context(session)})


@router.get("/goals", response_class=HTMLResponse)
def goals_page(request: Request, project_id: int = 0, session: Session = Depends(get_session)):
    return _render(request, "goals.html", {"preselect_project": project_id, **views.goals_context(session)})


@router.get("/goals/{goal_id}", response_class=HTMLResponse)
def goal_page(goal_id: int, request: Request, session: Session = Depends(get_session)):
    goal = session.get(Goal, goal_id)
    if goal is None:
        raise HTTPException(status_code=404, detail="Goal not found")
    return _render(request, "goal.html", views.goal_context(session, goal))


@router.get("/publications", response_class=HTMLResponse)
def publications_page(request: Request, project: int = 0, days: int = 30,
                      session: Session = Depends(get_session)):
    days = days if days in (7, 30, 90, 365) else 30
    return _render(request, "publications.html", views.publications_context(session, project, days))


@router.get("/publications/{brief_id}", response_class=HTMLResponse)
def publication_page(brief_id: int, request: Request, session: Session = Depends(get_session)):
    brief = session.get(Brief, brief_id)
    if brief is None:
        raise HTTPException(status_code=404, detail="Post not found")
    return _render(request, "publication.html", views.publication_context(session, brief))


@router.get("/tiktok", response_class=HTMLResponse)
def tiktok_page(request: Request, connected: int = 0, session: Session = Depends(get_session)):
    """TikTok pipeline: proven Shorts → the TikTok account, checklist and numbers in one place."""
    from app.services import tiktok_pipeline
    ctx = tiktok_pipeline.page_context(session, load_settings())
    return _render(request, "tiktok.html", {"ctx": ctx, "just_connected": bool(connected)})


@router.get("/guide", response_class=HTMLResponse)
def guide_page(request: Request):
    """The marketing playbook: static text, edited in the template."""
    return _render(request, "guide.html", {})


@router.get("/steam/traffic", response_class=HTMLResponse)
def steam_traffic_page(request: Request, session: Session = Depends(get_session)):
    origin = str(request.base_url).rstrip("/")
    return _render(request, "steam_traffic.html", views.steam_traffic_context(session, origin))


@router.get("/steam/traffic/import", response_class=HTMLResponse)
def steam_traffic_import_page(request: Request):
    """Where the bookmarklet lands; the page posts its URL fragment to the API."""
    return _render(request, "steam_import.html", {})


@router.get("/utm", response_class=HTMLResponse)
def utm_page(request: Request, project: int = 0, campaign: str = "", set: int = 0, saved: int = 0,
             source: str = "", medium: str = "", session: Session = Depends(get_session)):
    """UTM link generator; the query string restores the form after a save."""
    ctx = views.utm_context(session, project, campaign, show_set=bool(set))
    return _render(request, "utm.html", {**ctx, "saved_id": saved, "pre_source": source, "pre_medium": medium})


@router.get("/outreach", response_class=HTMLResponse)
def outreach_page(request: Request, project: int = 0, msg: str = "", session: Session = Depends(get_session)):
    """Creator outreach: similar games → YouTube/Twitch creators → letters with Steam keys."""
    from app.services import outreach, outreach_discovery
    ctx = outreach.page_context(session, project)
    yt_key = bool((load_settings().get("yt_api_key") or "").strip())
    return _render(request, "outreach.html", {**ctx, "msg": msg[:300], "yt_api_key_set": yt_key,
                                              "search_calls": outreach_discovery.search_calls_today(session),
                                              "search_limit": outreach_discovery.SEARCH_DAILY_LIMIT})
