"""Updates: the banner on every page, the Settings → Updates section, and what the tray asks (see services/updates.py)."""

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from app import __version__
from app.routers.api.common import _e, templates_api
from app.services import updates
from app.services.settings import load_settings, save_settings

router = APIRouter()

_COLORS = {  # background, border, text
    "available": ("#dbeafe", "#2563eb", "#1e3a8a"),
    "git": ("#e0e7ff", "#6366f1", "#312e81"),
    "progress": ("#fef3c7", "#d97706", "#92400e"),
    "need_token": ("#fef3c7", "#d97706", "#92400e"),
    "done": ("#dcfce7", "#16a34a", "#14532d"),
    "failed": ("#fee2e2", "#dc2626", "#991b1b"),
}


def _notes(latest: dict) -> tuple[str, str]:
    """(the "What's new" link, the notes block it opens)."""
    page = str(latest.get("page") or "")
    if not page.startswith("https://github.com/"):  # it comes from GitHub's API; anything else isn't linked
        page = ""
    more = (f"<div style='margin-top:6px'><a href='{_e(page)}' target='_blank' rel='noopener'>The release on GitHub ↗</a>"
            f"</div>") if page else ""
    block = (f"<div class='ip-notes' hidden style='flex-basis:100%;white-space:pre-wrap;margin-top:8px;"
             f"padding-top:8px;border-top:1px solid currentColor'>{_e(latest.get('notes') or 'No notes.')}{more}</div>")
    link = ("<a href='#' onclick=\"this.closest('#update_alert').querySelector('.ip-notes').toggleAttribute('hidden');"
            "return false\">What's new</a>")
    return link, block


def banner_html() -> str:
    from app.desktop import is_supervised
    b = updates.banner()
    if not b:
        return ""
    kind = b["kind"]
    bg, border, fg = _COLORS[kind]
    attrs, right, after = "", "", ""
    if kind in ("available", "git"):
        latest = b["latest"]
        link, after = _notes(latest)
        if kind == "git":
            text = f"IndiePulse v{_e(latest['version'])} is available — {link} · this is a git checkout: update it with " \
                   f"<code>git pull</code>"
        elif is_supervised():
            text = f"IndiePulse v{_e(latest['version'])} is available — {link} · "
            right = ("<button type='button' class='btn btn-primary' hx-post='/api/updates/install?view=banner' "
                     "hx-target='#update_alert' hx-swap='outerHTML'>Install</button>")
        else:
            text = f"IndiePulse v{_e(latest['version'])} is available — {link} · start IndiePulse from its tray icon " \
                   f"(IndiePulse.bat) to install it"
    elif kind == "progress":
        text = _e(b["text"])
        attrs = " hx-get='/api/updates/banner' hx-trigger='every 5s' hx-swap='outerHTML'"
    elif kind == "need_token":
        text = f"Update check: {_e(b['text'])}"
        right = "<a href='/accounts#section_updates' class='btn' style='text-decoration:none'>Settings → Updates</a>"
    else:  # done / failed
        text = _e(b["text"])
        right = ("<button type='button' class='btn' title='Hide' hx-post='/api/updates/dismiss' "
                 "hx-target='#update_alert' hx-swap='outerHTML'>×</button>")
    return (f"<div id='update_alert' role='status'{attrs} style='margin:0 0 16px;padding:10px 14px;background:{bg};"
            f"border-left:4px solid {border};border-radius:6px;color:{fg};display:flex;gap:12px;align-items:center;"
            f"flex-wrap:wrap'><span>⬆ {text}</span>"
            f"<span style='margin-left:auto;display:flex;gap:8px'>{right}</span>{after}</div>")


@router.get("/updates/banner", response_class=HTMLResponse)
def updates_banner():
    """Banner on every page: a newer IndiePulse, an install under way or how it went (files only, no network)."""
    return HTMLResponse(banner_html())


@router.post("/updates/dismiss", response_class=HTMLResponse)
def updates_dismiss():
    updates.mark_progress(seen=True)
    return HTMLResponse(banner_html())


@router.get("/updates/busy")
def updates_busy():
    """For the tray: an install waits while this isn't empty."""
    return {"busy": updates.busy()}


def _panel(request: Request, note: str = "", error: bool = False):
    from app.desktop import is_supervised
    from app.services.youtube_setup import fmt_when
    cfg = load_settings()
    c = updates.config(cfg)
    state = updates.load_state()
    token = c["token"]
    return templates_api.TemplateResponse("partials/updates_panel.html", {
        "request": request,
        "version": __version__,
        "mode": c["mode"],
        "repo": c["repo"],
        "token_hint": ("•••" + token[-4:]) if token else "",
        "git": updates.is_git_checkout(),
        "supervised": is_supervised(),
        "state": state,
        "checked": fmt_when(state["checked_at"]) if state.get("checked_at") else "",
        "latest": state.get("latest") if isinstance(state.get("latest"), dict) else None,
        "available": updates.available(state),
        "progress": updates.progress(),
        "note": note,
        "note_error": error,
    })


@router.get("/updates/panel", response_class=HTMLResponse)
def updates_panel(request: Request):
    return _panel(request)


@router.post("/updates/save", response_class=HTMLResponse)
def updates_save(request: Request, update_mode: str = Form(""), update_repo: str = Form(""),
                 update_token: str = Form(""), clear_token: str = Form("")):
    changes = {}
    if update_mode.strip() in updates.MODES:
        changes["update_mode"] = update_mode.strip()
    repo = update_repo.strip().strip("/")
    if repo.startswith("https://github.com/"):  # pasted from the address bar
        repo = repo[len("https://github.com/"):].removesuffix(".git").strip("/")
    if repo:
        if not updates.valid_repo(repo):
            return _panel(request, "The repository is owner/name, e.g. Sargonius/IndiePulseNew.", error=True)
        changes["update_repo"] = repo
    if clear_token:
        changes["update_token"] = ""
    elif update_token.strip():
        changes["update_token"] = update_token.strip()
    save_settings(changes)
    return _panel(request, "Saved.")


@router.post("/updates/check", response_class=HTMLResponse)
def updates_check(request: Request):
    """Settings → «Check now»: asks GitHub from the server (works without the tray too)."""
    state = updates.check()
    if state.get("error"):
        return _panel(request, state["error"], error=True)
    latest = updates.available(state)
    if latest:
        return _panel(request, f"IndiePulse {latest['version']} is available.")
    if state.get("latest") is None:
        return _panel(request, f"{state.get('repo')} has no releases yet.")
    return _panel(request, f"IndiePulse {__version__} is the latest version.")


@router.post("/updates/install", response_class=HTMLResponse)
def updates_install(request: Request, view: str = "banner"):
    """The banner's and Settings' Install: the tray does it (only it can stop the server)."""
    problem = updates.request_install()
    if view == "panel":
        return _panel(request, problem or "Asked the tray to install it: IndiePulse restarts in a moment.",
                      error=bool(problem))
    if problem:
        return HTMLResponse(f"<div id='update_alert' role='alert' style='margin:0 0 16px;padding:10px 14px;"
                            f"background:#fee2e2;border-left:4px solid #dc2626;border-radius:6px;color:#991b1b'>"
                            f"{_e(problem)}</div>")
    return HTMLResponse(banner_html())
